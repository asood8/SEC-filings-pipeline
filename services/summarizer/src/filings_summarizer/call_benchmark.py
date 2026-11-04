"""`filings-call-benchmark`: summarize the labeled earnings-call exchanges with each model and
score them (config/call_summary_eval.json, ADR-0022).

Each labeled exchange is found by company, call day and analyst among the stored transcripts
(`filings-discovery fetch-transcripts` first), and summarized with the exchange prompt the
nightly job uses, answers capped the same way. Scores use the 8-K benchmark's matcher: the
question as the headline, the answer as the summary. Read the summaries behind any score change
before trusting it (runbook).
"""

import argparse
import json
import logging
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from pymongo import MongoClient
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import DatabaseSettings, MongoSettings
from filings_common.db import CallTranscript, Company, create_engine, session_factory
from filings_common.mongo import TranscriptStore
from filings_common.names import company_name
from filings_common.summaries import FilingSummary
from filings_common.transcripts import Exchange, split
from filings_summarizer.benchmark import _providers
from filings_summarizer.calls import (
    CALL_PROMPT_VERSION,
    EXCHANGE_MAX_TOKENS,
    EXCHANGE_SCHEMA,
    EXCHANGE_SYSTEM,
    CallContext,
    ExchangeSummary,
    exchange_prompt,
)
from filings_summarizer.grounding import ungrounded_numbers
from filings_summarizer.llm import LlmError, LlmProvider
from filings_summarizer.quality import (
    ExchangeLabels,
    LabelError,
    QualityScore,
    load_call_labels,
    quality_stats,
    score_summary,
)

logger = logging.getLogger("filings_summarizer.call_benchmark")

DEFAULT_CALL_LABELS = Path("config/call_summary_eval.json")


@dataclass(frozen=True)
class Case:
    labels: ExchangeLabels
    context: CallContext
    exchange: Exchange


@dataclass
class ModelRun:
    provider: str
    model: str
    seconds: list[float] = field(default_factory=list)
    failed: int = 0
    ungrounded: int = 0  # summaries stating a number the exchange doesn't
    scores: list[QualityScore] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        stats = quality_stats(self.scores)
        ordered = sorted(self.seconds)
        return {
            "model": f"{self.provider}:{self.model}",
            "summaries": len(self.seconds) + self.failed,
            "failed": self.failed,
            "median_s": round(statistics.median(ordered), 1) if ordered else None,
            "max_s": round(ordered[-1], 1) if ordered else None,
            "fact_recall": stats["fact_recall"],
            "forbidden_share": stats["forbidden_share"],
            "ungrounded": self.ungrounded,
        }


def load_cases(
    sessions: sessionmaker[Session], store: TranscriptStore, labels: Sequence[ExchangeLabels]
) -> tuple[list[Case], list[str]]:
    """The labeled exchanges found among the stored transcripts, and the ones that weren't."""
    cases, missing = [], []
    with sessions() as session:
        transcripts = {
            (ticker, day): (url, name)
            for ticker, day, url, name in session.execute(
                sa.select(
                    Company.ticker,
                    CallTranscript.call_date,
                    CallTranscript.source_url,
                    Company.name,
                ).join(Company, Company.cik == CallTranscript.cik)
            )
        }
    for label in labels:
        where = f"{label.ticker} {label.call_date} {label.analyst}"
        found = transcripts.get((label.ticker, label.call_date))
        text = store.text(found[0]) if found else None
        exchange = (
            next((e for e in split(text).exchanges if e.analyst == label.analyst), None)
            if text
            else None
        )
        if found is None or exchange is None:
            missing.append(where)
            continue
        cases.append(
            Case(
                label, CallContext(company_name(found[1]), label.ticker, label.call_date), exchange
            )
        )
    return cases, missing


def run(providers: Sequence[LlmProvider], cases: Sequence[Case], runs: int) -> list[ModelRun]:
    results = []
    for provider in providers:
        result = ModelRun(provider.name, provider.model)
        for case in cases:
            for _ in range(runs):
                start = time.monotonic()
                try:
                    text = provider.generate_json(
                        EXCHANGE_SYSTEM,
                        exchange_prompt(case.context, case.exchange),
                        EXCHANGE_SCHEMA,
                    )
                    summary = ExchangeSummary.model_validate_json(text)
                except (LlmError, ValidationError) as exc:
                    result.failed += 1
                    result.summaries.append({"analyst": case.labels.analyst, "error": str(exc)})
                    logger.warning("%s %s: %s", provider.model, case.labels.analyst, exc)
                    continue
                result.seconds.append(time.monotonic() - start)
                said = f"{summary.question} {summary.answer}"
                flagged = ungrounded_numbers(said, case.exchange.text)
                result.ungrounded += bool(flagged)
                score = score_summary(
                    FilingSummary.model_construct(
                        headline=summary.question, summary=summary.answer, key_points=[]
                    ),
                    case.labels,
                )
                result.scores.append(score)
                result.summaries.append(
                    {
                        "ticker": case.labels.ticker,
                        "call_date": case.labels.call_date.isoformat(),
                        "analyst": case.labels.analyst,
                        "question": summary.question,
                        "answer": summary.answer,
                        "ungrounded_numbers": flagged,
                        "missing": score.missing,
                        "forbidden": score.forbidden,
                    }
                )
                logger.info(
                    "%s %s: %d/%d facts",
                    provider.model,
                    case.labels.analyst,
                    score.stated,
                    score.facts,
                )
        results.append(result)
    return results


def render(rows: Sequence[dict[str, Any]]) -> str:
    columns = list(rows[0])
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += [
        "| " + " | ".join("" if r[c] is None else str(r[c]) for c in columns) + " |" for r in rows
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="a model served at LLM_BASE_URL (repeat to compare several)",
    )
    parser.add_argument("--gemini", action="store_true", help="also run Gemini (GEMINI_API_KEY)")
    parser.add_argument("--labels", type=Path, default=DEFAULT_CALL_LABELS)
    parser.add_argument("--runs", type=int, default=1, help="summaries per exchange and model")
    parser.add_argument("--out", type=Path, default=None, help="write every summary to this JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    try:
        labels = load_call_labels(args.labels)
    except LabelError as exc:
        parser.error(str(exc))
    providers = _providers(parser, args, max_output_tokens=EXCHANGE_MAX_TOKENS)
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    mongo_settings = MongoSettings()
    mongo: MongoClient[dict[str, Any]] = MongoClient(mongo_settings.mongodb_url.get_secret_value())
    try:
        cases, missing = load_cases(
            session_factory(engine),
            TranscriptStore(mongo[mongo_settings.mongodb_database]),
            labels,
        )
    finally:
        mongo.close()
        engine.dispose()
    if missing:
        logger.warning("not among the stored transcripts (fetch them first): %s", missing)
    if not cases:
        print("no labeled exchange found; run filings-discovery fetch-transcripts", file=sys.stderr)
        return 1
    results = run(providers, cases, args.runs)
    print(render([r.row() for r in results]))
    if args.out is not None:
        args.out.write_text(
            json.dumps(
                {
                    "prompt_version": CALL_PROMPT_VERSION,
                    "results": [r.row() | {"summaries": r.summaries} for r in results],
                },
                indent=1,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
