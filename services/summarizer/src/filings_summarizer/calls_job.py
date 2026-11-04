"""`filings-summarize-calls`: summarize the stored earnings-call transcripts that have no summary
at the current prompt version (ADR-0022). The second step of the nightly call-transcripts job,
after `filings-discovery fetch-transcripts`.

Newest calls first, until the time budget runs out; whatever is left waits for the next night. A
call whose summary fails (every provider failed) is left for the next night too. Re-running
writes nothing twice: the same provider, model and prompt version replace their row.

A call's first summary is announced on `calls.summarized` (through the outbox, in the same
transaction) so the notifier alerts about it; later summaries of the call are not.
"""

import argparse
import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

import redis
import sqlalchemy as sa
from opentelemetry import trace
from pymongo import MongoClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import (
    DatabaseSettings,
    GeminiSettings,
    LlmSettings,
    MongoSettings,
    RedisSettings,
)
from filings_common.db import CallSummary, CallTranscript, Company, create_engine, session_factory
from filings_common.events import CallSummarized, Topic
from filings_common.mongo import TranscriptStore
from filings_common.names import company_name
from filings_common.outbox import enqueue
from filings_common.telemetry import setup_telemetry
from filings_summarizer.calls import (
    CALL_PROMPT_VERSION,
    EXCHANGE_MAX_TOKENS,
    TAKEAWAYS_MAX_TOKENS,
    CallContext,
    ExchangeSummary,
    summarize_call,
    takeaways_from_json,
)
from filings_summarizer.llm import FailoverChain, LlmError
from filings_summarizer.summarizers import build_providers

logger = logging.getLogger("filings_summarizer.calls")
tracer = trace.get_tracer(__name__)

DEFAULT_BUDGET_SECONDS = 3 * 60 * 60  # within the job's 4-hour deadline


@dataclass
class CallsReport:
    summarized: list[str] = field(default_factory=list)  # "TICKER date"
    failed: list[str] = field(default_factory=list)
    left: int = 0  # not reached within the budget


@dataclass(frozen=True)
class _Pending:
    transcript_id: int
    cik: int
    source_url: str
    context: CallContext


def pending(session: Session, prompt_version: int = CALL_PROMPT_VERSION) -> list[_Pending]:
    """Stored transcripts without a summary at `prompt_version`, newest call first."""
    summarized = sa.exists().where(
        CallSummary.transcript_id == CallTranscript.id,
        CallSummary.prompt_version == prompt_version,
    )
    rows = session.execute(
        sa.select(
            CallTranscript.id,
            CallTranscript.cik,
            CallTranscript.source_url,
            CallTranscript.call_date,
            Company.ticker,
            Company.name,
        )
        .join(Company, Company.cik == CallTranscript.cik)
        .where(~summarized, CallTranscript.exchanges > 0)
        .order_by(CallTranscript.call_date.desc(), CallTranscript.id)
    ).all()
    return [
        _Pending(id_, cik, url, CallContext(company_name(name), ticker, day))
        for id_, cik, url, day, ticker, name in rows
    ]


def summarize_calls(
    sessions: sessionmaker[Session],
    store: TranscriptStore,
    exchange_chain: FailoverChain,
    takeaways_chain: FailoverChain,
    *,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> CallsReport:
    report = CallsReport()
    deadline = clock() + budget_seconds
    with sessions() as session:
        calls = pending(session)
    for position, call in enumerate(calls):
        label = f"{call.context.ticker} {call.context.call_date}"
        if clock() >= deadline:
            report.left = len(calls) - position
            logger.info("time budget used: %d calls left for the next run", report.left)
            break
        text = store.text(call.source_url)
        if text is None:
            logger.warning("%s: transcript text missing from MongoDB (%s)", label, call.source_url)
            report.failed.append(label)
            continue
        with tracer.start_as_current_span("summarize call", attributes={"filings.call": label}):
            try:
                result = summarize_call(exchange_chain, takeaways_chain, call.context, text)
            except LlmError as exc:
                logger.warning("%s: not summarized this run: %s", label, exc)
                report.failed.append(label)
                continue
        if result is None:
            continue
        with sessions.begin() as session:
            first = not session.scalar(
                sa.select(sa.exists().where(CallSummary.transcript_id == call.transcript_id))
            )
            values: dict[str, Any] = {
                "takeaways": result.takeaways.model_dump(),
                "exchanges": result.exchanges,
                "ungrounded_numbers": result.ungrounded_numbers,
                "ungrounded_names": result.ungrounded_names,
            }
            summary_id = session.execute(
                pg_insert(CallSummary)
                .values(
                    transcript_id=call.transcript_id,
                    provider=result.provider,
                    model=result.model,
                    prompt_version=CALL_PROMPT_VERSION,
                    **values,
                )
                .on_conflict_do_update(
                    index_elements=["transcript_id", "provider", "model", "prompt_version"],
                    set_=values | {"created_at": sa.func.now()},
                )
                .returning(CallSummary.id)
            ).scalar_one()
            if first:  # announced once, for alerts; a later summary of the call isn't
                enqueue(
                    session,
                    Topic.CALLS_SUMMARIZED,
                    CallSummarized(
                        transcript_id=call.transcript_id, summary_id=summary_id, cik=call.cik
                    ),
                )
        logger.info("%s: %d exchanges summarized", label, len(result.exchanges))
        report.summarized.append(label)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filings-summarize-calls")
    parser.add_argument(
        "--budget-minutes",
        type=float,
        default=DEFAULT_BUDGET_SECONDS / 60,
        help="stop starting new calls after this long (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    telemetry = setup_telemetry("filings-summarize-calls")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        return _run(args.budget_minutes * 60)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        telemetry.shutdown()


def _exit_on_sigterm(signum: int, frame: FrameType | None) -> None:
    logger.warning("stopping: received SIGTERM")
    raise SystemExit(128 + signum)


def _run(budget_seconds: float) -> int:
    llm, gemini = LlmSettings(), GeminiSettings()
    redis_client = redis.Redis.from_url(RedisSettings().redis_url)
    exchange_providers = build_providers(
        llm, gemini, redis_client, max_output_tokens=EXCHANGE_MAX_TOKENS
    )
    if not exchange_providers:
        logger.warning("no LLM provider configured (LLM_MODEL, GEMINI_API_KEY); nothing to do")
        redis_client.close()
        return 0
    exchange_chain = FailoverChain(exchange_providers, validate=ExchangeSummary.model_validate_json)
    takeaways_chain = FailoverChain(
        build_providers(llm, gemini, redis_client, max_output_tokens=TAKEAWAYS_MAX_TOKENS),
        validate=takeaways_from_json,
    )
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    mongo_settings = MongoSettings()
    mongo: MongoClient[dict[str, Any]] = MongoClient(mongo_settings.mongodb_url.get_secret_value())
    try:
        report = summarize_calls(
            session_factory(engine),
            TranscriptStore(mongo[mongo_settings.mongodb_database]),
            exchange_chain,
            takeaways_chain,
            budget_seconds=budget_seconds,
        )
    finally:
        mongo.close()
        engine.dispose()
        redis_client.close()
    logger.info(
        "summarized %d calls, %d failed, %d left for the next run",
        len(report.summarized),
        len(report.failed),
        report.left,
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
