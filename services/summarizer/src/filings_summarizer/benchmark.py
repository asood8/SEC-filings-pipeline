"""Benchmark 8-K summarization across models (`filings-benchmark`): speed and checkable quality.

Runs the 8-Ks already collected (parsed payloads from MongoDB) through each model with the exact
production prompt and schema, and reports per model: latency, failures (errors or answers that
don't match the schema), and how often a summary contains numbers the filing doesn't (the
grounding check). Every summary is written to a JSON file, so quality can also be judged by reading
them side by side; numbers alone can't catch a wrong but well-formed claim.

Models are served by the OpenAI-compatible endpoint in LLM_BASE_URL (Ollama locally, llama.cpp on
the production VM), so the same command compares models on either machine. `--gemini` adds the
Gemini free tier as a reference point (it counts against the free daily quota).
"""

import argparse
import json
import logging
import math
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import redis
import sqlalchemy as sa
from pydantic import ValidationError
from pymongo import MongoClient

from filings_common.config import (
    DatabaseSettings,
    GeminiSettings,
    LlmSettings,
    MongoSettings,
    RedisSettings,
)
from filings_common.db import Company, Filing, create_engine, session_factory
from filings_common.mongo import ParsedFilingStore
from filings_common.names import company_name
from filings_common.parsed import Form8K
from filings_common.ratelimit import RedisTokenBucket
from filings_common.summaries import FilingSummary
from filings_summarizer import form8k_prompt
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.grounding import ungrounded_numbers
from filings_summarizer.llm import (
    GeminiProvider,
    LlmError,
    LlmProvider,
    OpenAICompatibleProvider,
)
from filings_summarizer.summarizers import GEMINI_RATE_LIMIT_KEY

logger = logging.getLogger("filings_summarizer.benchmark")


@dataclass(frozen=True)
class Case:
    """One 8-K to summarize: its parsed payload and the context the prompt needs."""

    form: Form8K
    context: FilingContext


@dataclass
class Attempt:
    accession_no: str
    seconds: float
    error: str | None = None
    summary: dict[str, Any] | None = None
    ungrounded: list[str] = field(default_factory=list)


@dataclass
class ModelResult:
    provider: str
    model: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def succeeded(self) -> list[Attempt]:
        return [a for a in self.attempts if a.error is None]

    def stats(self) -> dict[str, Any]:
        ok = self.succeeded
        seconds = sorted(a.seconds for a in ok)
        words = [len(str(a.summary["summary"]).split()) for a in ok if a.summary is not None]
        return {
            "runs": len(self.attempts),
            "failed": len(self.attempts) - len(ok),
            "median_s": round(statistics.median(seconds), 2) if seconds else None,
            "p95_s": round(_percentile(seconds, 0.95), 2) if seconds else None,
            "ungrounded_share": round(sum(bool(a.ungrounded) for a in ok) / len(ok), 2)
            if ok
            else None,
            "ungrounded_per_summary": round(statistics.mean(len(a.ungrounded) for a in ok), 2)
            if ok
            else None,
            "summary_words": round(statistics.mean(words)) if words else None,
        }


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of already sorted values."""
    index = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[min(index, len(sorted_values) - 1)]


def summarize_once(provider: LlmProvider, case: Case) -> Attempt:
    """One production-identical summary, timed. Never raises: failures are recorded."""
    prompt = form8k_prompt.build_prompt(case.form, case.context)
    started = time.perf_counter()
    try:
        text = provider.generate_json(form8k_prompt.SYSTEM, prompt, form8k_prompt.SUMMARY_SCHEMA)
        summary = FilingSummary.model_validate_json(text)
    except (LlmError, ValidationError) as exc:
        return Attempt(
            case.context.accession_no,
            time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
    elapsed = time.perf_counter() - started
    summary_text = " ".join([summary.headline, summary.summary, *summary.key_points])
    return Attempt(
        case.context.accession_no,
        elapsed,
        summary=summary.model_dump(),
        ungrounded=ungrounded_numbers(summary_text, prompt),
    )


def run_benchmark(
    providers: Sequence[LlmProvider], cases: Sequence[Case], *, runs: int = 1, warmup: bool = True
) -> list[ModelResult]:
    """Every case through every provider, `runs` times each. With `warmup`, one untimed call
    first loads the model, so the first case isn't charged for it."""
    results = []
    for provider in providers:
        result = ModelResult(provider.name, provider.model)
        if warmup and cases:
            summarize_once(provider, cases[0])
        for case in cases:
            for _ in range(runs):
                attempt = summarize_once(provider, case)
                result.attempts.append(attempt)
                logger.info(
                    "%s %s %.1fs %s",
                    provider.model,
                    case.context.accession_no,
                    attempt.seconds,
                    attempt.error or f"{len(attempt.ungrounded)} ungrounded",
                )
        results.append(result)
    return results


def render_table(results: Sequence[ModelResult]) -> str:
    """A Markdown table, one row per model."""
    lines = [
        "| Model | Runs | Failed | Median s | p95 s | Summaries with ungrounded numbers "
        "| Ungrounded per summary | Summary words |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        s = result.stats()
        share = "n/a" if s["ungrounded_share"] is None else f"{s['ungrounded_share']:.0%}"
        lines.append(
            f"| {result.provider}: {result.model} | {s['runs']} | {s['failed']} "
            f"| {_cell(s['median_s'])} | {_cell(s['p95_s'])} | {share} "
            f"| {_cell(s['ungrounded_per_summary'])} | {_cell(s['summary_words'])} |"
        )
    return "\n".join(lines)


def _cell(value: object) -> str:
    return "n/a" if value is None else str(value)


def load_cases(limit: int | None) -> list[Case]:
    """The collected 8-Ks, oldest first: parsed payloads from MongoDB, context from Postgres."""
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    with session_factory(engine)() as session:
        query = (
            sa.select(Filing, Company)
            .join(Company, Company.cik == Filing.cik)
            .where(Filing.form_type == "8-K")
            .order_by(Filing.filed_at, Filing.accession_no)
        )
        rows = session.execute(query).all()
    engine.dispose()
    mongo_settings = MongoSettings()
    mongo: MongoClient[dict[str, Any]] = MongoClient(mongo_settings.mongodb_url.get_secret_value())
    store = ParsedFilingStore(mongo[mongo_settings.mongodb_database])
    cases = []
    for filing, company in rows:
        document = store.get(filing.accession_no)
        if document is None:
            continue
        cases.append(
            Case(
                Form8K.model_validate(document["parsed"]),
                FilingContext(
                    accession_no=filing.accession_no,
                    form_type=filing.form_type,
                    company_name=company_name(company.name),  # as the summarizer has it
                    ticker=company.ticker,
                    filed_at=filing.filed_at,
                ),
            )
        )
        if limit is not None and len(cases) >= limit:
            break
    mongo.close()
    return cases


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="a model served at LLM_BASE_URL (repeat to compare several)",
    )
    parser.add_argument("--gemini", action="store_true", help="also run Gemini (GEMINI_API_KEY)")
    parser.add_argument("--limit", type=int, default=None, help="at most this many 8-Ks")
    parser.add_argument("--runs", type=int, default=1, help="summaries per 8-K and model")
    parser.add_argument("--out", type=Path, default=None, help="write every summary to this JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    llm = LlmSettings()
    providers: list[LlmProvider] = [
        OpenAICompatibleProvider(
            name=llm.provider_name, base_url=llm.base_url, model=model, timeout=llm.timeout_seconds
        )
        for model in args.model
    ]
    if args.gemini:
        gemini = GeminiSettings()
        if gemini.api_key is None:
            parser.error("--gemini needs GEMINI_API_KEY")
        limiter = RedisTokenBucket(
            redis.Redis.from_url(RedisSettings().redis_url),
            GEMINI_RATE_LIMIT_KEY,
            gemini.requests_per_minute / 60,
        )
        providers.append(
            GeminiProvider(
                api_key=gemini.api_key.get_secret_value(),
                model=gemini.model,
                timeout=gemini.timeout_seconds,
                limiter=limiter,
            )
        )
    if not providers:
        parser.error("give at least one --model, or --gemini")

    cases = load_cases(args.limit)
    if not cases:
        print("no parsed 8-Ks found; run the pipeline first", file=sys.stderr)
        return 1
    logger.info("benchmarking %d model(s) on %d 8-K(s)", len(providers), len(cases))
    results = run_benchmark(providers, cases, runs=args.runs)
    print(render_table(results))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "prompt_version": form8k_prompt.PROMPT_VERSION,
                    "results": [
                        {"provider": r.provider, "model": r.model, "stats": r.stats()}
                        | {"attempts": [asdict(a) for a in r.attempts]}
                        for r in results
                    ],
                },
                indent=2,
                default=str,
            )
        )
        logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
