import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from filings_summarizer.llm import (
    CircuitBreaker,
    FailoverChain,
    GeminiProvider,
    LlmError,
    LlmUnavailableError,
    OpenAICompatibleProvider,
    RateLimitedError,
)

SCHEMA = {"type": "object", "properties": {"headline": {"type": "string"}}}
ANSWER = '{"headline": "Acme names a CFO"}'


def transport(
    *responses: httpx2.Response | Exception,
) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []
    pending = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        response = pending.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx2.MockTransport(handler), requests


def gemini_ok(text: str = ANSWER) -> httpx2.Response:
    return httpx2.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


class CountingLimiter:
    calls = 0

    def acquire(self) -> float:
        self.calls += 1
        return 0.0


# --- Gemini --------------------------------------------------------------------------------------


def test_gemini_request_shape_and_answer() -> None:
    mock, requests = transport(gemini_ok())
    limiter = CountingLimiter()
    provider = GeminiProvider(
        api_key="secret-key", model="gemini-test", timeout=5, limiter=limiter, transport=mock
    )
    assert provider.generate_json("be brief", "summarize this", SCHEMA) == ANSWER

    [request] = requests
    assert request.url.path.endswith("/models/gemini-test:generateContent")
    assert request.headers["x-goog-api-key"] == "secret-key"
    assert "secret-key" not in str(request.url)  # never in the URL, so never in logs
    body = json.loads(request.content)
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert body["contents"][0]["parts"][0]["text"] == "summarize this"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert limiter.calls == 1


def test_gemini_429_reports_its_retry_delay() -> None:
    error = {
        "error": {
            "code": 429,
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "36s"}],
        }
    }
    mock, _ = transport(httpx2.Response(429, json=error))
    provider = GeminiProvider(api_key="k", model="m", timeout=5, transport=mock)
    with pytest.raises(RateLimitedError) as excinfo:
        provider.generate_json("s", "p", SCHEMA)
    assert excinfo.value.retry_after == 36


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(503, json={"error": {"message": "high demand"}}), "503"),
        (httpx2.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "SAFETY"),
        (httpx2.ConnectError("no route"), "unreachable"),
    ],
)
def test_gemini_failures(response: httpx2.Response | Exception, message: str) -> None:
    mock, _ = transport(response)
    provider = GeminiProvider(api_key="k", model="m", timeout=5, transport=mock)
    with pytest.raises(LlmError, match=message):
        provider.generate_json("s", "p", SCHEMA)


# --- OpenAI-compatible (Ollama, llama.cpp) -------------------------------------------------------


def test_openai_compatible_request_shape_and_answer() -> None:
    reply = {"choices": [{"message": {"role": "assistant", "content": ANSWER}}]}
    mock, requests = transport(httpx2.Response(200, json=reply))
    provider = OpenAICompatibleProvider(
        name="ollama", base_url="http://llm:11434/v1/", model="small", timeout=5, transport=mock
    )
    assert provider.generate_json("be brief", "summarize this", SCHEMA) == ANSWER

    [request] = requests
    assert str(request.url) == "http://llm:11434/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "small"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA


def test_openai_compatible_server_down() -> None:
    mock, _ = transport(httpx2.ConnectError("connection refused"))
    provider = OpenAICompatibleProvider(
        name="ollama", base_url="http://llm/v1", model="m", timeout=5, transport=mock
    )
    with pytest.raises(LlmError, match="ollama unreachable"):
        provider.generate_json("s", "p", SCHEMA)


# --- Circuit breaker -----------------------------------------------------------------------------


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_breaker_opens_after_repeated_failures_and_retries_after_cooldown() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, cooldown=60, clock=clock)
    breaker.record_failure()
    assert breaker.allow()
    breaker.record_failure()
    assert not breaker.allow()
    clock.now = 60
    assert breaker.allow()  # one trial call
    breaker.record_failure()  # the trial failed: open again
    assert not breaker.allow()
    clock.now = 120
    breaker.record_success()
    assert breaker.allow()


def test_rate_limit_opens_the_breaker_for_the_requested_delay() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=5, cooldown=10, clock=clock)
    breaker.record_failure(retry_after=36)
    clock.now = 35
    assert not breaker.allow()
    clock.now = 36
    assert breaker.allow()


# --- Failover ------------------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, name: str, *answers: str | Exception) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.answers = list(answers)
        self.calls = 0

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def chain(
    *providers: FakeProvider, validate: Callable[[str], object] | None = None
) -> FailoverChain:
    return FailoverChain(
        providers,
        breaker_factory=lambda: CircuitBreaker(threshold=2, cooldown=60),
        validate=validate,
    )


def test_uses_the_first_provider_that_answers() -> None:
    local = FakeProvider("ollama", LlmError("down"))
    gemini = FakeProvider("gemini", ANSWER)
    answer = chain(local, gemini).generate_json("s", "p", SCHEMA)
    assert (answer.provider, answer.model, answer.text) == ("gemini", "gemini-model", ANSWER)


def test_a_failing_provider_is_skipped_once_its_breaker_opens() -> None:
    local = FakeProvider("ollama", LlmError("down"), LlmError("down"))
    gemini = FakeProvider("gemini", ANSWER, ANSWER, ANSWER)
    failover = chain(local, gemini)
    for _ in range(3):
        assert failover.generate_json("s", "p", SCHEMA).provider == "gemini"
    assert local.calls == 2  # the third call skipped it: the breaker is open


def test_an_invalid_answer_fails_over_to_the_next_provider() -> None:
    local = FakeProvider("ollama", "not json")
    gemini = FakeProvider("gemini", ANSWER)
    answer = chain(local, gemini, validate=json.loads).generate_json("s", "p", SCHEMA)
    assert answer.provider == "gemini"


def test_all_providers_failing_is_a_transient_error() -> None:
    failover = chain(
        FakeProvider("ollama", LlmError("down")),
        FakeProvider("gemini", RateLimitedError("429", 30)),
    )
    with pytest.raises(LlmUnavailableError, match=r"ollama.*gemini"):
        failover.generate_json("s", "p", SCHEMA)


# --- Tracing -------------------------------------------------------------------------------------


def test_each_provider_call_is_a_span_with_provider_model_and_outcome_but_no_text(
    spans: InMemorySpanExporter,
) -> None:
    prompt, answer = "Summarize: Acme's CFO resigned.", '{"headline": "Acme CFO quits"}'
    failover = chain(
        FakeProvider("ollama", "not json"),  # answers, but invalid
        FakeProvider("gemini", LlmError(f"gemini returned 500: {prompt}")),
        FakeProvider("backup", answer),
        validate=json.loads,
    )
    assert failover.generate_json("be brief", prompt, SCHEMA).text == answer

    calls = spans.get_finished_spans()
    assert [(s.name, s.kind) for s in calls] == [
        ("chat ollama-model", SpanKind.CLIENT),
        ("chat gemini-model", SpanKind.CLIENT),
        ("chat backup-model", SpanKind.CLIENT),
    ]
    assert [(s.attributes or {})["filings.outcome"] for s in calls] == ["invalid", "error", "ok"]
    assert dict(calls[2].attributes or {}) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "backup",
        "gen_ai.request.model": "backup-model",
        "filings.outcome": "ok",
    }
    assert calls[1].status.status_code is StatusCode.ERROR
    assert calls[1].status.description == "filings_summarizer.llm.LlmError"
    for span in calls:  # neither the prompt nor any answer, even inside an error
        assert "Acme" not in span.to_json()
        assert "not json" not in span.to_json()


def test_provider_spans_carry_token_counts_and_rate_limit_wait(
    spans: InMemorySpanExporter,
) -> None:
    reply = {
        "choices": [{"message": {"content": ANSWER}}],
        "usage": {"prompt_tokens": 812, "completion_tokens": 64},
    }
    local_mock, _ = transport(httpx2.Response(200, json=reply))
    local = OpenAICompatibleProvider(
        name="ollama", base_url="http://llm/v1", model="small", timeout=5, transport=local_mock
    )
    gemini_reply = gemini_ok().json() | {
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 70}
    }
    gemini_mock, _ = transport(httpx2.Response(200, json=gemini_reply))
    limiter = CountingLimiter()
    gemini = GeminiProvider(
        api_key="k", model="g", timeout=5, limiter=limiter, transport=gemini_mock
    )
    for provider in (local, gemini):
        failover = FailoverChain(
            [provider], breaker_factory=lambda: CircuitBreaker(threshold=2, cooldown=60)
        )
        failover.generate_json("s", "p", SCHEMA)

    local_span, gemini_span = (s.attributes or {} for s in spans.get_finished_spans())
    assert (local_span["gen_ai.usage.input_tokens"], local_span["gen_ai.usage.output_tokens"]) == (
        812,
        64,
    )
    assert gemini_span["gen_ai.usage.input_tokens"] == 900
    assert gemini_span["filings.rate_limit.wait_seconds"] == 0.0


# --- Metrics -------------------------------------------------------------------------------------


def test_breaker_reports_when_it_opens() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, cooldown=60, clock=clock)
    assert [breaker.record_failure(), breaker.record_failure()] == [False, True]
    clock.now = 60
    assert breaker.record_failure()  # the trial call failed: it opens again


def test_provider_calls_failovers_and_breaker_opens_are_measured(measured: Any) -> None:
    local = FakeProvider("ollama", LlmError("down"), "not json")
    gemini = FakeProvider("gemini", ANSWER, ANSWER, ANSWER)
    failover = chain(local, gemini, validate=json.loads)
    for _ in range(3):  # the third call skips ollama: its breaker is open
        assert failover.generate_json("s", "p", SCHEMA).provider == "gemini"

    def calls(provider: str, outcome: str) -> int:
        attributes = {"gen_ai.provider.name": provider, "filings.outcome": outcome}
        return int(measured.count("filings.llm.duration", attributes))

    assert (calls("ollama", "error"), calls("ollama", "invalid"), calls("gemini", "ok")) == (
        1,
        1,
        3,
    )
    assert measured.attribute_sets("filings.llm.failovers") == [{"gen_ai.provider.name": "gemini"}]
    assert measured.total("filings.llm.failovers") == 3
    assert measured.attribute_sets("filings.llm.circuit_breaker.opens") == [
        {"gen_ai.provider.name": "ollama"}
    ]
    assert measured.total("filings.llm.circuit_breaker.opens") == 1


def test_an_answer_from_the_first_provider_is_no_failover(measured: Any) -> None:
    chain(FakeProvider("ollama", ANSWER), FakeProvider("gemini")).generate_json("s", "p", SCHEMA)
    assert measured.count("filings.llm.duration", {"gen_ai.provider.name": "ollama"}) == 1
    assert measured.total("filings.llm.failovers") == 0
