"""Benchmark 8-K summarization across models (`filings-benchmark`): speed and checkable quality.

Runs the 8-Ks already collected (parsed payloads from MongoDB) through each model with the exact
production prompt and schema, and reports per model:
- latency, and failures (errors or answers that don't match the schema);
- fact recall and forbidden claims, against the labeled set in `config/summary_eval.json`
  (`quality.py`): the share of the facts a correct summary states that the summary does state, and
  how often it makes a claim the filing contradicts;
- how often a summary contains numbers the filing doesn't (the grounding check).
Every summary, with what it missed and any forbidden claim, is written to a JSON file, so the
scores can be checked by reading them side by side.

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
from collections.abc import Callable, Mapping, Sequence
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
from filings_summarizer.quality import (
    DEFAULT_LABELS,
    FilingLabels,
    LabelError,
    QualityScore,
    load_labels,
    quality_stats,
    score_summary,
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
    # Against the filing's labels; None when it has none.
    quality: QualityScore | None = None


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

    def quality_stats(self) -> dict[str, Any]:
        """Fact recall and forbidden claims over the successful summaries of labeled filings."""
        return quality_stats(a.quality for a in self.succeeded if a.quality is not None)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of already sorted values."""
    index = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[min(index, len(sorted_values) - 1)]


def summarize_once(
    provider: LlmProvider, case: Case, labels: FilingLabels | None = None
) -> Attempt:
    """One production-identical summary, timed and scored. Never raises: failures are recorded."""
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
        quality=score_summary(summary, labels) if labels is not None else None,
    )


def run_benchmark(
    providers: Sequence[LlmProvider],
    cases: Sequence[Case],
    *,
    runs: int = 1,
    warmup: bool = True,
    labels: Mapping[str, FilingLabels] | None = None,
    progress: Callable[[list[ModelResult]], None] | None = None,
) -> list[ModelResult]:
    """Every case through every provider, `runs` times each, scored against `labels` (by
    accession number) where a filing has them. With `warmup`, one untimed call first loads the
    model, so the first case isn't charged for it. `progress` gets the results so far after every
    attempt, so a long run that is interrupted keeps what it measured."""
    labels = labels or {}
    results: list[ModelResult] = []
    for provider in providers:
        result = ModelResult(provider.name, provider.model)
        results.append(result)
        if warmup and cases:
            summarize_once(provider, cases[0])
        for case in cases:
            for _ in range(runs):
                attempt = summarize_once(provider, case, labels.get(case.context.accession_no))
                result.attempts.append(attempt)
                logger.info(
                    "%s %s %.1fs %s",
                    provider.model,
                    case.context.accession_no,
                    attempt.seconds,
                    attempt.error or _outcome(attempt),
                )
                if progress is not None:
                    progress(results)
    return results


def _outcome(attempt: Attempt) -> str:
    outcome = f"{len(attempt.ungrounded)} ungrounded"
    if attempt.quality is not None:
        q = attempt.quality
        outcome += f", facts {q.stated}/{q.facts}, {len(q.forbidden)} forbidden"
    return outcome


def render_table(results: Sequence[ModelResult]) -> str:
    """A Markdown table, one row per model."""
    lines = [
        "| Model | Runs | Failed | Median s | p95 s | Fact recall "
        "| Summaries with forbidden claims | Summaries with ungrounded numbers "
        "| Ungrounded per summary | Summary words |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        s = result.stats()
        q = result.quality_stats()
        lines.append(
            f"| {result.provider}: {result.model} | {s['runs']} | {s['failed']} "
            f"| {_cell(s['median_s'])} | {_cell(s['p95_s'])} | {_share(q['fact_recall'])} "
            f"| {_share(q['forbidden_share'])} | {_share(s['ungrounded_share'])} "
            f"| {_cell(s['ungrounded_per_summary'])} | {_cell(s['summary_words'])} |"
        )
    return "\n".join(lines)


def _cell(value: object) -> str:
    return "n/a" if value is None else str(value)


def _share(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def load_cases(limit: int | None, accessions: Sequence[str] = ()) -> list[Case]:
    """The collected 8-Ks (or just `accessions`), oldest first: parsed payloads from MongoDB,
    context from Postgres."""
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    with session_factory(engine)() as session:
        query = (
            sa.select(Filing, Company)
            .join(Company, Company.cik == Filing.cik)
            .where(Filing.form_type == "8-K")
            .order_by(Filing.filed_at, Filing.accession_no)
        )
        if accessions:
            query = query.where(Filing.accession_no.in_(accessions))
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


def rescore(saved: dict[str, Any], labels: Mapping[str, FilingLabels]) -> list[ModelResult]:
    """Results saved with --out, scored again against `labels` without calling any model, so a
    change to the labels doesn't mean summarizing everything again. Timing, failures, and
    grounding are kept as saved."""
    results = []
    for saved_result in saved["results"]:
        result = ModelResult(saved_result["provider"], saved_result["model"])
        for a in saved_result["attempts"]:
            filing_labels = labels.get(a["accession_no"])
            quality = None
            if a["summary"] is not None and filing_labels is not None:
                quality = score_summary(FilingSummary.model_validate(a["summary"]), filing_labels)
            result.attempts.append(
                Attempt(
                    a["accession_no"],
                    a["seconds"],
                    error=a["error"],
                    summary=a["summary"],
                    ungrounded=a["ungrounded"],
                    quality=quality,
                )
            )
        results.append(result)
    return results


def write_results(
    path: Path, prompt_version: object, labels_path: Path | None, results: Sequence[ModelResult]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "prompt_version": prompt_version,
        "labels": None if labels_path is None else str(labels_path),
        "results": [
            {
                "provider": r.provider,
                "model": r.model,
                "stats": r.stats() | r.quality_stats(),
                "attempts": [asdict(a) for a in r.attempts],
            }
            for r in results
        ],
    }
    # Replaced whole, never left half-written: it is rewritten after every summary.
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=2, default=str))
    partial.replace(path)


def _providers(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[LlmProvider]:
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
    return providers


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
    parser.add_argument(
        "--accession", action="append", default=[], help="only this 8-K (repeat for several)"
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=None,
        help=f"facts and forbidden claims to score summaries against (default: {DEFAULT_LABELS},"
        " skipped if it isn't there)",
    )
    parser.add_argument("--runs", type=int, default=1, help="summaries per 8-K and model")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write every summary to this JSON (rewritten after each one, so an interrupted run"
        " keeps what it measured)",
    )
    parser.add_argument(
        "--rescore",
        type=Path,
        default=None,
        metavar="RESULTS",
        help="score the summaries in a JSON written by --out again (after the labels change),"
        " without calling any model",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    labels_path: Path | None = args.labels or DEFAULT_LABELS
    try:
        labels = load_labels(labels_path) if labels_path is not None else {}
    except LabelError as exc:
        if args.labels is not None:
            parser.error(str(exc))
        logger.warning("no quality score: %s", exc)
        labels, labels_path = {}, None

    prompt_version: object = form8k_prompt.PROMPT_VERSION
    if args.rescore is not None:
        try:
            saved = json.loads(args.rescore.read_text(encoding="utf-8"))
            results = rescore(saved, labels)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(f"{args.rescore} is not a benchmark result: {exc!r}")
        prompt_version = saved.get("prompt_version")
    else:
        providers = _providers(parser, args)
        cases = load_cases(args.limit, args.accession)
        if not cases:
            print("no parsed 8-Ks found; run the pipeline first", file=sys.stderr)
            return 1
        unlabeled = sum(c.context.accession_no not in labels for c in cases)
        if labels and unlabeled:
            logger.warning("%d 8-K(s) have no labels, so no quality score", unlabeled)
        logger.info("benchmarking %d model(s) on %d 8-K(s)", len(providers), len(cases))
        out = args.out

        def save(so_far: list[ModelResult]) -> None:
            write_results(out, prompt_version, labels_path, so_far)

        results = run_benchmark(
            providers,
            cases,
            runs=args.runs,
            labels=labels,
            progress=save if out is not None else None,
        )
    print(render_table(results))
    if args.out is not None:
        write_results(args.out, prompt_version, labels_path, results)
        logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
