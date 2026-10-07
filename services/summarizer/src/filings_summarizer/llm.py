"""LLM providers behind one small interface, plus failover between them.

Each provider returns JSON text matching a JSON schema. Providers are plain HTTP clients:
- `OpenAICompatibleProvider` talks to any server with OpenAI's chat-completions API (Ollama in
  development, llama.cpp's server in production);
- `GeminiProvider` talks to Google's Gemini API (free tier).

`FailoverChain` tries providers in order. Each has a circuit breaker, so a provider that keeps
failing is skipped until a cooldown passes; a 429 opens the breaker for at least the provider's
requested retry delay. When every provider is unavailable, `LlmUnavailableError` (a transient
error) lets the consume loop retry later; meanwhile the backlog waits in the topic.

Each provider call is a CLIENT span with the provider, model, outcome, and token counts. Spans
never hold prompt or answer text, and record errors by type only: their messages can quote the
answer (a failed validation) or the provider's response. Metrics: each call's duration by provider
and outcome, answers that came from a fallback provider, and circuit breakers opening.
"""

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from filings_common import metrics, telemetry
from filings_common.ratelimit import RateLimiter

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


class LlmError(Exception):
    """A provider failed to produce an answer."""


class RateLimitedError(LlmError):
    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LlmUnavailableError(LlmError):
    """No provider could answer right now; retrying later may succeed."""


class LlmProvider(Protocol):
    name: str
    model: str

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        """Return JSON text matching `schema`. Raise LlmError (or a subclass) on failure."""
        ...


def _raise_for_status(response: httpx2.Response, provider: str) -> None:
    if response.status_code == 429:
        raise RateLimitedError(f"{provider} rate limited", _retry_after(response))
    if response.status_code >= 400:
        raise LlmError(f"{provider} returned {response.status_code}: {response.text[:300]}")


def _record_token_usage(usage: object, input_key: str, output_key: str) -> None:
    """Token counts, when the provider reports them, on the current LLM call's span."""
    if not isinstance(usage, dict):
        return
    span = trace.get_current_span()
    for attribute, key in (
        (telemetry.GEN_AI_INPUT_TOKENS, input_key),
        (telemetry.GEN_AI_OUTPUT_TOKENS, output_key),
    ):
        if isinstance(count := usage.get(key), int):
            span.set_attribute(attribute, count)


def _retry_after(response: httpx2.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header and header.isdigit():
        return float(header)
    # Gemini puts it in the error details: {"@type": "...RetryInfo", "retryDelay": "36s"}.
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', response.text)
    return float(match[1]) if match else None


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        timeout: float,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._http = httpx2.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        try:
            response = self._http.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "filing_summary", "schema": schema, "strict": True},
                    },
                    "temperature": 0.2,
                },
            )
        except httpx2.TransportError as exc:
            raise LlmError(f"{self.name} unreachable: {exc!r}") from exc
        _raise_for_status(response, self.name)
        try:
            body = response.json()
            content: str = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(f"{self.name} returned an unexpected response") from exc
        _record_token_usage(body.get("usage"), "prompt_tokens", "completion_tokens")
        return content


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float,
        limiter: RateLimiter | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._limiter = limiter
        # The key travels in a header, never in the URL, so it can't leak into logs.
        self._http = httpx2.Client(
            base_url=GEMINI_API,
            headers={"x-goog-api-key": api_key},
            timeout=timeout,
            transport=transport,
        )

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        if self._limiter is not None:
            waited = self._limiter.acquire()
            trace.get_current_span().set_attribute(telemetry.RATE_LIMIT_WAIT, waited)
        try:
            response = self._http.post(
                f"/models/{self.model}:generateContent",
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseJsonSchema": schema,
                        "temperature": 0.2,
                    },
                },
            )
        except httpx2.TransportError as exc:
            raise LlmError(f"gemini unreachable: {exc!r}") from exc
        _raise_for_status(response, "gemini")
        body = response.json()
        _record_token_usage(body.get("usageMetadata"), "promptTokenCount", "candidatesTokenCount")
        candidates = body.get("candidates") or []
        if not candidates:
            reason = (body.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LlmError(f"gemini returned no answer ({reason})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise LlmError(f"gemini returned an empty answer ({candidates[0].get('finishReason')})")
        return text


class CircuitBreaker:
    """Closed → open after `threshold` consecutive failures → one trial call after `cooldown`."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._open_until: float | None = None

    def allow(self) -> bool:
        return self._open_until is None or self._clock() >= self._open_until

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = None

    def record_failure(self, retry_after: float | None = None) -> bool:
        """Count a failure. Returns True if the breaker opened because of it."""
        self._failures += 1
        half_open = self._open_until is not None  # a trial call after the cooldown failed
        if retry_after is not None or half_open or self._failures >= self._threshold:
            self._open_until = self._clock() + max(self._cooldown, retry_after or 0)
            return True
        return False


@dataclass(frozen=True)
class Answer:
    text: str
    provider: str
    model: str


class FailoverChain:
    def __init__(
        self,
        providers: Sequence[LlmProvider],
        *,
        breaker_factory: Callable[[], CircuitBreaker] = CircuitBreaker,
        validate: Callable[[str], object] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = list(providers)
        self._breakers = {p.name: breaker_factory() for p in providers}
        self._validate = validate

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> Answer:
        """The first valid answer, trying providers in order.

        An answer that fails `validate` (malformed or off-schema JSON) counts as that provider
        failing, so the next one is tried.
        """
        failures = []
        for position, provider in enumerate(self._providers):
            breaker = self._breakers[provider.name]
            if not breaker.allow():
                failures.append(f"{provider.name}: circuit open")
                trace.get_current_span().add_event(
                    "LLM provider skipped: circuit open", {telemetry.GEN_AI_PROVIDER: provider.name}
                )
                continue
            with telemetry.redacted_span(
                tracer,
                f"chat {provider.model}",
                kind=SpanKind.CLIENT,
                attributes={
                    telemetry.GEN_AI_OPERATION: "chat",
                    telemetry.GEN_AI_PROVIDER: provider.name,
                    telemetry.GEN_AI_MODEL: provider.model,
                },
            ) as span:
                answered, opened = False, False
                started = time.monotonic()
                try:
                    text = provider.generate_json(system, prompt, schema)
                    answered = True
                    if self._validate is not None:
                        self._validate(text)
                except RateLimitedError as exc:
                    opened = breaker.record_failure(retry_after=exc.retry_after)
                    failures.append(f"{provider.name}: {exc}")
                    outcome = "rate_limited"
                    telemetry.record_error(span, exc, redact=True)
                except Exception as exc:  # any provider failure means: try the next provider
                    opened = breaker.record_failure()
                    failures.append(f"{provider.name}: {exc!r}"[:300])
                    outcome = "invalid" if answered else "error"
                    telemetry.record_error(span, exc, redact=True)
                else:
                    breaker.record_success()
                    outcome = "ok"
                span.set_attribute(telemetry.OUTCOME, outcome)
                provider_attribute = {telemetry.GEN_AI_PROVIDER: provider.name}
                metrics.LLM_DURATION.record(
                    time.monotonic() - started, {**provider_attribute, telemetry.OUTCOME: outcome}
                )
                if opened:
                    metrics.LLM_BREAKER_OPENS.add(1, provider_attribute)
                if outcome == "ok":
                    if position > 0:
                        metrics.LLM_FAILOVERS.add(1, provider_attribute)
                    return Answer(text, provider.name, provider.model)
            logger.warning("LLM provider %s failed; trying the next one", provider.name)
        raise LlmUnavailableError("; ".join(failures))
