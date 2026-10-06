"""Summarizers, by EDGAR form type.

A summarizer turns a parsed payload into a `FilingSummary` and says who wrote it: provider, model,
and prompt (or template) version, all stored with the summary. Form 4s use a deterministic
template. 8-Ks will use LLM providers behind a failover summarizer, which reports whichever
provider actually answered. Forms without a summarizer are acknowledged and stay `parsed`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from filings_common.consumer import PermanentError
from filings_common.parsed import Form4
from filings_common.summaries import FilingSummary
from filings_summarizer import form4_template


@dataclass(frozen=True)
class SummaryResult:
    summary: FilingSummary
    provider: str
    model: str
    prompt_version: int


class Summarizer(Protocol):
    def summarize(self, payload: dict[str, Any]) -> SummaryResult:
        """Summarize a parsed payload. Raise PermanentError if retrying cannot help."""
        ...


class Form4TemplateSummarizer:
    def summarize(self, payload: dict[str, Any]) -> SummaryResult:
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


SUMMARIZERS: Mapping[str, Summarizer] = {"4": Form4TemplateSummarizer()}
