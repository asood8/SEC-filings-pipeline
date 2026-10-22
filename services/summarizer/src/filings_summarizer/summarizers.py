"""Summarizers, by EDGAR form type.

A summarizer turns a parsed payload into a `FilingSummary` and says who wrote it: provider, model,
and prompt (or template) version, all stored with the summary.
- Form 4: a deterministic template.
- 8-K: an LLM, through a failover chain (local model first, then Gemini); the provider that
  actually answered is the one recorded.

Forms without a summarizer are acknowledged and stay `parsed`.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import redis
from pydantic import ValidationError

from filings_common.config import GeminiSettings, LlmSettings
from filings_common.consumer import PermanentError
from filings_common.parsed import Form4, Form8K
from filings_common.ratelimit import RedisTokenBucket
from filings_common.summaries import FilingSummary
from filings_summarizer import form4_template, form8k_prompt
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.grounding import ungrounded_names, ungrounded_numbers
from filings_summarizer.llm import (
    FailoverChain,
    GeminiProvider,
    LlmProvider,
    OpenAICompatibleProvider,
)

logger = logging.getLogger(__name__)

GEMINI_RATE_LIMIT_KEY = "ratelimit:gemini"


@dataclass(frozen=True)
class SummaryResult:
    summary: FilingSummary
    provider: str
    model: str
    prompt_version: int
    # From the grounding checks (LLM summaries only); None when not checked.
    ungrounded_numbers: list[str] | None = None
    ungrounded_names: list[str] | None = None


class Summarizer(Protocol):
    def summarize(self, payload: dict[str, Any], context: FilingContext) -> SummaryResult:
        """Summarize a parsed payload. Raise PermanentError if retrying cannot help."""
        ...


class Form4TemplateSummarizer:
    def summarize(self, payload: dict[str, Any], context: FilingContext) -> SummaryResult:
        try:
            form = Form4.model_validate(payload)
            summary = form4_template.summarize_form4(form)
        except (ValidationError, ValueError) as exc:
            # The template is deterministic: the same payload fails the same way every time.
            raise PermanentError(f"cannot summarize Form 4 payload: {exc}") from exc
        return SummaryResult(
            summary=summary,
            provider=form4_template.PROVIDER,
            model=form4_template.MODEL,
            prompt_version=form4_template.TEMPLATE_VERSION,
        )


class Form8KLlmSummarizer:
    def __init__(self, chain: FailoverChain) -> None:
        self._chain = chain

    def summarize(self, payload: dict[str, Any], context: FilingContext) -> SummaryResult:
        try:
            form = Form8K.model_validate(payload)
        except ValidationError as exc:
            raise PermanentError(f"cannot read 8-K payload: {exc}") from exc
        prompt = form8k_prompt.build_prompt(form, context)
        # LlmUnavailableError propagates: it is transient, so the consume loop retries later.
        answer = self._chain.generate_json(
            form8k_prompt.SYSTEM, prompt, form8k_prompt.SUMMARY_SCHEMA
        )
        summary = FilingSummary.model_validate_json(answer.text)
        # Checked against exactly what the model saw, header and trimming included.
        summary_text = " ".join([summary.headline, summary.summary, *summary.key_points])
        return SummaryResult(
            summary=summary,
            provider=answer.provider,
            model=answer.model,
            prompt_version=form8k_prompt.PROMPT_VERSION,
            ungrounded_numbers=ungrounded_numbers(summary_text, prompt),
            ungrounded_names=ungrounded_names(
                summary.headline, summary.summary, summary.key_points, prompt
            ),
        )


def build_summarizers(
    llm: LlmSettings, gemini: GeminiSettings, redis_client: redis.Redis
) -> dict[str, Summarizer]:
    summarizers: dict[str, Summarizer] = {"4": Form4TemplateSummarizer()}
    providers: list[LlmProvider] = []
    if llm.model:
        providers.append(
            OpenAICompatibleProvider(
                name=llm.provider_name,
                base_url=llm.base_url,
                model=llm.model,
                timeout=llm.timeout_seconds,
            )
        )
    if gemini.api_key is not None:
        limiter = RedisTokenBucket(
            redis_client, GEMINI_RATE_LIMIT_KEY, gemini.requests_per_minute / 60
        )
        providers.append(
            GeminiProvider(
                api_key=gemini.api_key.get_secret_value(),
                model=gemini.model,
                timeout=gemini.timeout_seconds,
                limiter=limiter,
            )
        )
    if providers:
        chain = FailoverChain(providers, validate=FilingSummary.model_validate_json)
        summarizers["8-K"] = Form8KLlmSummarizer(chain)
        logger.info("8-K summaries via %s", " -> ".join(f"{p.name}:{p.model}" for p in providers))
    else:
        logger.warning("no LLM provider configured (LLM_MODEL, GEMINI_API_KEY); 8-Ks stay parsed")
    return summarizers
