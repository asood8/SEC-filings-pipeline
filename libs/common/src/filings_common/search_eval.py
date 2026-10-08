"""`filings-search-eval`: measure search quality on a labeled query set, so ranking changes can be
compared with numbers instead of impressions.

Each labeled query (a JSON file listing, per query, the accession numbers of the relevant filings)
runs through keyword, semantic, and hybrid search with the functions the API calls, the same
candidates and fusion included, against the configured database and embedding model. Per mode it
reports, averaged over queries:

- **Recall@5 and recall@10:** the share of a query's relevant filings in its top 5 or 10.
- **MRR:** the reciprocal rank of the first relevant filing within the top 20 (the API's default
  page), 0 when none is there.
- **Form 4 share@5:** for queries whose relevant filings are all other forms, how many of the 5
  top slots Form 4s take. Form 4s are most of the filings, and they crowd out the 8-Ks people
  were looking for.

It only reads: its database transactions are read-only. Without a working embedding model, the
semantic and hybrid modes are skipped and keyword search is still measured.
"""

import argparse
import json
import logging
import statistics
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import DatabaseSettings, EmbeddingSettings
from filings_common.db import Filing, FilingEmbedding, create_engine, session_factory
from filings_common.embeddings import Embedder, EmbeddingError, embedder_from_settings
from filings_common.search import hybrid_search, keyword_search, semantic_search

logger = logging.getLogger("filings_common.search_eval")

Mode = Literal["keyword", "semantic", "hybrid"]
MODES: tuple[Mode, ...] = ("keyword", "semantic", "hybrid")
# The API's default page size: how deep each ranking is read.
DEPTH = 20
DEFAULT_LABELS = Path("config/search_eval.json")

AccessionNo = Annotated[str, StringConstraints(pattern=r"^\d{10}-\d{2}-\d{6}$")]


class LabelError(ValueError):
    """The label file is missing, isn't JSON, or doesn't have the expected shape."""


class LabeledQuery(BaseModel):
    """A query and the filings a person searching with it wants to find."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=200)  # the API's limits
    relevant: tuple[AccessionNo, ...] = Field(min_length=1)
    # Why these filings, so the judgment can be checked and redone.
    note: str = ""

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("relevant")
    @classmethod
    def _no_repeats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("lists an accession number twice")
        return value


class LabelFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # How the labels were chosen.
    description: str = ""
    queries: list[LabeledQuery] = Field(min_length=1)

    @field_validator("queries")
    @classmethod
    def _unique_queries(cls, value: list[LabeledQuery]) -> list[LabeledQuery]:
        seen: set[str] = set()
        for labeled in value:
            key = labeled.query.strip().lower()
            if key in seen:
                raise ValueError(f"query {labeled.query!r} appears twice")
            seen.add(key)
        return value


def load_labels(path: Path) -> list[LabeledQuery]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabelError(f"can't read {path}: {exc.strerror or exc}") from exc
    try:
        return LabelFile.model_validate_json(text).queries
    except ValidationError as exc:
        raise LabelError(f"{path} is not a valid label file:\n{exc}") from exc


# Metrics. A ranking is a list of accession numbers, best first.


def recall_at(ranking: Sequence[str], relevant: Collection[str], k: int) -> float:
    """The share of the relevant filings that are in the top k."""
    return len(set(ranking[:k]) & set(relevant)) / len(relevant)


def first_relevant_rank(ranking: Sequence[str], relevant: Collection[str]) -> int | None:
    return next((rank for rank, a in enumerate(ranking, start=1) if a in relevant), None)


def reciprocal_rank(ranking: Sequence[str], relevant: Collection[str]) -> float:
    """1 / the rank of the first relevant filing, 0 if there is none."""
    rank = first_relevant_rank(ranking, relevant)
    return 0.0 if rank is None else 1.0 / rank


def form4_share(ranking: Sequence[str], form_types: Mapping[str, str], k: int = 5) -> float:
    """How many of the k top slots Form 4s take, out of k (an empty slot isn't one)."""
    return sum(form_types.get(a) == "4" for a in ranking[:k]) / k


@dataclass(frozen=True)
class QueryResult:
    query: str
    relevant: int
    recall_at_5: float
    recall_at_10: float
    reciprocal_rank: float
    # None for queries some of whose relevant filings are Form 4s.
    form4_share_at_5: float | None
    # Rank of the first relevant filing, None if it isn't in the top DEPTH.
    first_relevant_rank: int | None
    top: list[str]


@dataclass(frozen=True)
class ModeResult:
    mode: Mode
    queries: list[QueryResult]

    def summary(self) -> dict[str, Any]:
        shares = [q.form4_share_at_5 for q in self.queries if q.form4_share_at_5 is not None]
        return {
            "mode": self.mode,
            "queries": len(self.queries),
            "recall_at_5": _mean(q.recall_at_5 for q in self.queries),
            "recall_at_10": _mean(q.recall_at_10 for q in self.queries),
            "mrr": _mean(q.reciprocal_rank for q in self.queries),
            "form4_share_at_5": _mean(shares),
            "form4_share_queries": len(shares),
        }


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(statistics.fmean(items), 3) if items else None


def score(
    mode: Mode,
    labels: Sequence[LabeledQuery],
    rankings: Sequence[Sequence[str]],
    form_types: Mapping[str, str],
) -> ModeResult:
    """Score one mode's rankings (one per labeled query, in order). `form_types` maps accession
    numbers to form types, for the relevant filings and the ranked ones."""
    results = []
    for labeled, ranking in zip(labels, rankings, strict=True):
        relevant = set(labeled.relevant)
        # Unknown filings (not in the database) count as Form 4s here: the share is only
        # measured where every relevant filing is known to be another form.
        no_form4 = all(form_types.get(a, "4") != "4" for a in relevant)
        results.append(
            QueryResult(
                query=labeled.query,
                relevant=len(relevant),
                recall_at_5=recall_at(ranking, relevant, 5),
                recall_at_10=recall_at(ranking, relevant, 10),
                reciprocal_rank=reciprocal_rank(ranking, relevant),
                form4_share_at_5=form4_share(ranking, form_types) if no_form4 else None,
                first_relevant_rank=first_relevant_rank(ranking, relevant),
                top=list(ranking[:10]),
            )
        )
    return ModeResult(mode, results)


# Running the searches.


def ranking(
    session: Session,
    mode: Mode,
    query: str,
    *,
    embedder: Embedder | None = None,
    vector: Sequence[float] = (),
) -> list[str]:
    """One query's top DEPTH accession numbers, searched the way the API does it. Semantic and
    hybrid need the embedder and the query's vector."""
    if mode == "keyword":
        hits = keyword_search(session, query, limit=DEPTH)
    elif embedder is None:
        raise ValueError(f"{mode} search needs an embedder")
    elif mode == "semantic":
        hits = semantic_search(session, vector, model=embedder.model, limit=DEPTH)
    else:
        hits = hybrid_search(session, query, vector, model=embedder.model, limit=DEPTH)
    return [h.accession_no for h in hits]


def form_types_of(session: Session, accession_nos: Collection[str]) -> dict[str, str]:
    rows = session.execute(
        sa.select(Filing.accession_no, Filing.form_type).where(
            Filing.accession_no.in_(list(accession_nos))
        )
    ).all()
    return {row.accession_no: row.form_type for row in rows}


@dataclass
class Evaluation:
    depth: int
    embedding_model: str | None
    results: list[ModeResult]
    # Modes that couldn't run, and why.
    skipped: dict[str, str]
    # Labeled filings the database doesn't have: labels made for another database.
    missing: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "embedding_model": self.embedding_model,
            "skipped": self.skipped,
            "missing": self.missing,
            "modes": [
                r.summary() | {"per_query": [asdict(q) for q in r.queries]} for r in self.results
            ],
        }


def evaluate(
    session: Session,
    labels: Sequence[LabeledQuery],
    *,
    modes: Sequence[Mode] = MODES,
    embedder: Embedder | None,
) -> Evaluation:
    """Run every labeled query in each mode and score the rankings. Semantic and hybrid are
    skipped, with the reason, when there is no embedder or it fails on any query."""
    vectors: dict[str, list[float]] = {}
    skipped: dict[str, str] = {}
    needs_vectors = [m for m in modes if m != "keyword"]
    if needs_vectors and embedder is None:
        skipped = dict.fromkeys(needs_vectors, "no embedding model (EMBEDDING_MODEL is unset)")
    elif needs_vectors and embedder is not None:
        try:
            vectors = {q.query: embedder.embed_query(q.query) for q in labels}
        except EmbeddingError as exc:
            skipped = dict.fromkeys(needs_vectors, f"the embedding model failed: {exc}")
        else:
            _warn_if_unembedded(session, embedder.model)
    rankings = {
        mode: [
            ranking(session, mode, q.query, embedder=embedder, vector=vectors.get(q.query, ()))
            for q in labels
        ]
        for mode in modes
        if mode not in skipped
    }
    labeled = {a for q in labels for a in q.relevant}
    ranked = {a for per_query in rankings.values() for r in per_query for a in r}
    form_types = form_types_of(session, labeled | ranked)
    return Evaluation(
        depth=DEPTH,
        embedding_model=embedder.model if embedder is not None else None,
        results=[score(mode, labels, r, form_types) for mode, r in rankings.items()],
        skipped=skipped,
        missing=sorted(labeled - form_types.keys()),
    )


def _warn_if_unembedded(session: Session, model: str) -> None:
    count = session.scalar(
        sa.select(sa.func.count())
        .select_from(FilingEmbedding)
        .where(FilingEmbedding.model == model)
    )
    if not count:
        logger.warning("no filings are embedded with %s: run filings-index first", model)


def read_only(engine: sa.Engine) -> sessionmaker[Session]:
    """Sessions whose transactions Postgres runs READ ONLY: an evaluation never writes."""
    return session_factory(engine.execution_options(postgresql_readonly=True))


# Reporting.


def render_summary(evaluation: Evaluation) -> str:
    """A Markdown table, one row per mode, then what couldn't be measured."""
    lines = [
        "| Mode | Queries | Recall@5 | Recall@10 | MRR | Form 4 share@5 |",
        "|---|---|---|---|---|---|",
    ]
    for result in evaluation.results:
        s = result.summary()
        share = (
            "n/a"
            if s["form4_share_at_5"] is None
            else f"{s['form4_share_at_5']:.0%} ({s['form4_share_queries']} queries)"
        )
        lines.append(
            f"| {s['mode']} | {s['queries']} | {_number(s['recall_at_5'])} "
            f"| {_number(s['recall_at_10'])} | {_number(s['mrr'])} | {share} |"
        )
    lines += [f"\n{mode}: skipped, {reason}" for mode, reason in evaluation.skipped.items()]
    if evaluation.missing:
        lines.append(
            f"\n{len(evaluation.missing)} labeled filings are not in this database, so the labels "
            "don't fit it: " + ", ".join(evaluation.missing)
        )
    return "\n".join(lines)


def render_per_query(result: ModeResult) -> str:
    """A Markdown table for one mode, one row per query."""
    lines = [
        f"#### {result.mode}",
        "",
        "| Query | Relevant | Recall@5 | Recall@10 | First relevant | Form 4s in top 5 |",
        "|---|---|---|---|---|---|",
    ]
    for q in result.queries:
        first = "-" if q.first_relevant_rank is None else str(q.first_relevant_rank)
        share = "n/a" if q.form4_share_at_5 is None else str(round(q.form4_share_at_5 * 5))
        lines.append(
            f"| {q.query} | {q.relevant} | {_number(q.recall_at_5)} | {_number(q.recall_at_10)} "
            f"| {first} | {share} |"
        )
    return "\n".join(lines)


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS,
        help=f"the labeled queries (default: {DEFAULT_LABELS})",
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=MODES,
        help="evaluate only this mode (repeat for several; default: all three)",
    )
    parser.add_argument(
        "--per-query", action="store_true", help="also print a table per mode, one row per query"
    )
    parser.add_argument("--json", type=Path, default=None, help="also write the results here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    try:
        labels = load_labels(args.labels)
    except LabelError as exc:
        print(exc, file=sys.stderr)
        return 2
    modes: list[Mode] = args.mode or list(MODES)
    settings = EmbeddingSettings()
    # The eval measures ranking, not latency: a busy model may take as long as it does for
    # documents, where the API would fall back to keywords.
    embedder = embedder_from_settings(
        settings.model_copy(update={"query_timeout_seconds": settings.timeout_seconds})
    )
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    try:
        with read_only(engine)() as session:
            evaluation = evaluate(session, labels, modes=modes, embedder=embedder)
    finally:
        engine.dispose()

    print(render_summary(evaluation))
    if args.per_query:
        for result in evaluation.results:
            print(f"\n{render_per_query(result)}")
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        report = {"labels": str(args.labels)} | evaluation.to_json()
        args.json.write_text(json.dumps(report, indent=2) + "\n")
        logger.info("wrote %s", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
