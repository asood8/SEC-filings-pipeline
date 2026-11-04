"""Summaries of earnings calls' Q&A, from the transcripts companies publish (ADR-0022).

A call runs about 10,000 words, beyond the small model's context, so it is summarized in two
steps: each Q&A exchange alone (who asked, about what, what management said), then the call's
takeaways from those summaries. The grounding checks run on each exchange against its own text,
and on the takeaways against the whole transcript.

Changing a prompt in any way that changes answers means bumping CALL_PROMPT_VERSION.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field

from filings_common.summaries import FilingSummary, OneLine
from filings_common.transcripts import Exchange, split
from filings_summarizer.form8k_prompt import SUMMARY_SCHEMA, truncate
from filings_summarizer.grounding import ungrounded_names, ungrounded_numbers
from filings_summarizer.llm import FailoverChain, LlmError

CALL_PROMPT_VERSION = 1

# Answers are capped: a small model can otherwise run on for thousands of tokens (measured in
# the pilot: one answer reached 4,300 tokens before the request timed out). The cap fits the
# longest summary the schema accepts (a 600-character question, a 1,500-character answer: about
# 550 tokens of JSON). At 400, the model's near-verbatim answer to a long reply was cut off.
EXCHANGE_MAX_TOKENS = 700
TAKEAWAYS_MAX_TOKENS = 700
MAX_EXCHANGE_CHARS = 12_000  # the longest real exchange is about 9,000 (1,300 words)
MAX_TAKEAWAYS_PROMPT_CHARS = 16_000

EXCHANGE_SYSTEM = """\
You summarize one question-and-answer exchange from a company's earnings call, for investors.
Use only what the transcript says. Never add outside knowledge, speculate, or give investment \
advice. Keep numbers, names, and dates exactly as spoken, and do not calculate figures the \
transcript does not state. Name the executives who answered.
Return JSON with:
- question: one sentence on what the analyst asked;
- answer: 1 to 3 sentences on what management said, with any figures they gave."""

EXCHANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "One sentence."},
        "answer": {"type": "string", "description": "1 to 3 sentences."},
    },
    "required": ["question", "answer"],
    "additionalProperties": False,
}

TAKEAWAYS_SYSTEM = """\
You summarize the question-and-answer part of a company's earnings call for investors, from \
short summaries of each exchange. Use only what the summaries say. Never add outside knowledge, \
speculate, or give investment advice. Keep numbers and names exactly as written.
Return JSON with:
- headline: one line, at most 120 characters, naming the company and the main theme of the \
questions;
- summary: 2 to 4 plain-English sentences;
- key_points: 3 to 5 short, concrete points (who said what, figures given)."""


MAX_QUESTION_CHARS = 600
MAX_ANSWER_CHARS = 1_500


def _clip(limit: int) -> AfterValidator:
    """Text past `limit` is cut at a word and marked: the small model often copies a long
    multi-part question or answer nearly word for word, which is long but not wrong, so it
    shouldn't count as a failure (and send the exchange to the next provider)."""

    def clip(text: str) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."

    return AfterValidator(clip)


class ExchangeSummary(BaseModel):
    question: Annotated[OneLine, Field(min_length=1), _clip(MAX_QUESTION_CHARS)]
    answer: Annotated[OneLine, Field(min_length=1), _clip(MAX_ANSWER_CHARS)]


@dataclass(frozen=True)
class CallContext:
    company_name: str
    ticker: str
    call_date: date


@dataclass
class CallResult:
    takeaways: FilingSummary
    provider: str  # of the takeaways
    model: str
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    ungrounded_numbers: list[str] = field(default_factory=list)
    ungrounded_names: list[str] = field(default_factory=list)


def _header(context: CallContext) -> str:
    return (
        f"Company: {context.company_name} ({context.ticker})\n"
        f"Earnings call on {context.call_date.isoformat()}\n"
    )


def exchange_prompt(context: CallContext, exchange: Exchange) -> str:
    return (
        f"{_header(context)}Analyst: {exchange.analyst}, {exchange.firm}\n\n"
        f"{truncate(exchange.text, MAX_EXCHANGE_CHARS)}"
    )


def takeaways_prompt(context: CallContext, exchanges: list[dict[str, Any]]) -> str:
    listing = "\n".join(
        f"- {x['analyst']} ({x['firm']}) asked: {x['question']} Answer: {x['answer']}"
        for x in exchanges
    )
    return f"{_header(context)}\n{truncate(listing, MAX_TAKEAWAYS_PROMPT_CHARS)}"


def summarize_call(
    exchange_chain: FailoverChain,
    takeaways_chain: FailoverChain,
    context: CallContext,
    text: str,
) -> CallResult | None:
    """Every exchange's summary, then the takeaways. None for a transcript with no Q&A."""
    transcript = split(text)
    if not transcript.exchanges:
        return None
    exchanges: list[dict[str, Any]] = []
    for exchange in transcript.exchanges:
        try:
            answer = exchange_chain.generate_json(
                EXCHANGE_SYSTEM, exchange_prompt(context, exchange), EXCHANGE_SCHEMA
            )
        except LlmError:
            # One exchange no provider could summarize doesn't hold up the call: it is listed
            # as failed, and the takeaways come from the others.
            exchanges.append({"analyst": exchange.analyst, "firm": exchange.firm, "failed": True})
            continue
        summary = ExchangeSummary.model_validate_json(answer.text)
        said = f"{summary.question} {summary.answer}"
        exchanges.append(
            {
                "analyst": exchange.analyst,
                "firm": exchange.firm,
                "question": summary.question,
                "answer": summary.answer,
                "provider": answer.provider,
                "model": answer.model,
                "ungrounded_numbers": ungrounded_numbers(said, exchange.text),
                "ungrounded_names": ungrounded_names("", said, [], exchange.text),
            }
        )
    summarized = [x for x in exchanges if not x.get("failed")]
    if len(summarized) * 2 < len(exchanges):
        raise LlmError(f"{len(exchanges) - len(summarized)} of {len(exchanges)} exchanges failed")
    answer = takeaways_chain.generate_json(
        TAKEAWAYS_SYSTEM, takeaways_prompt(context, summarized), SUMMARY_SCHEMA
    )
    takeaways = FilingSummary.model_validate_json(answer.text)
    said = " ".join([takeaways.headline, takeaways.summary, *takeaways.key_points])
    return CallResult(
        takeaways=takeaways,
        provider=answer.provider,
        model=answer.model,
        exchanges=exchanges,
        ungrounded_numbers=ungrounded_numbers(said, text),
        ungrounded_names=ungrounded_names(
            takeaways.headline, takeaways.summary, takeaways.key_points, text
        ),
    )
