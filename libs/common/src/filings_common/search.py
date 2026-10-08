"""Search over filings: one document per filing, built from its current summary.

- Keyword search uses Postgres full-text search: a weighted `tsvector` (headline and company above
  body) queried with `websearch_to_tsquery`, so quotes, OR, and -term work as people expect.
- Semantic search compares a query embedding with document embeddings (pgvector, cosine).
- Hybrid search merges both rankings with reciprocal rank fusion, which needs no score
  calibration between the two: each list contributes 1 / (k + rank).

`filings-search-eval` (search_eval.py) measures ranking changes on a labeled query set.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from filings_common.db import Company, Filing, FilingEmbedding, FilingSearch, Summary
from filings_common.embeddings import Embedder, EmbeddingError
from filings_common.summaries import FilingSummary

logger = logging.getLogger(__name__)

RRF_K = 60
# How many candidates each method contributes before hybrid fusion.
HYBRID_CANDIDATES = 50
# Words people search with that summaries don't necessarily contain.
FORM_LABELS = {
    "4": "Form 4 insider trading report",
    "8-K": "Form 8-K current report",
}


@dataclass(frozen=True)
class SearchDocument:
    title: str
    company: str
    body: str

    def text(self) -> str:
        """The text that gets embedded."""
        return f"{self.title}\n{self.company}\n{self.body}"


@dataclass(frozen=True)
class SearchHit:
    accession_no: str
    score: float


def build_document(
    summary: FilingSummary, *, company_name: str, ticker: str | None, form_type: str
) -> SearchDocument:
    label = FORM_LABELS.get(form_type, f"Form {form_type}")
    return SearchDocument(
        title=summary.headline,
        company=f"{company_name} {ticker}" if ticker else company_name,
        body="\n".join([summary.summary, *summary.key_points, label]),
    )


def upsert_document(
    session: Session, accession_no: str, summary_id: int, document: SearchDocument
) -> None:
    values = {
        "summary_id": summary_id,
        "title": document.title,
        "company": document.company,
        "body": document.body,
    }
    stmt = pg_insert(FilingSearch).values(accession_no=accession_no, **values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FilingSearch.accession_no],
            set_={**values, "updated_at": sa.func.now()},
        )
    )


def upsert_embedding(
    session: Session, accession_no: str, model: str, embedding: Sequence[float]
) -> None:
    stmt = pg_insert(FilingEmbedding).values(
        accession_no=accession_no, model=model, embedding=list(embedding)
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FilingEmbedding.accession_no, FilingEmbedding.model],
            set_={"embedding": stmt.excluded.embedding, "updated_at": sa.func.now()},
        )
    )


def _filters(*, form_type: str | None, cik: int | None) -> list[sa.ColumnElement[bool]]:
    clauses = []
    if form_type is not None:
        clauses.append(Filing.form_type == form_type)
    if cik is not None:
        clauses.append(Filing.cik == cik)
    return clauses


def _is_plain(text: str) -> bool:
    """No search syntax (quotes, OR, -word), so relaxing AND to OR keeps the user's meaning."""
    words = text.split()
    return (
        '"' not in text
        and "or" not in {w.lower() for w in words}
        and not any(w.startswith("-") for w in words)
    )


def keyword_search(
    session: Session,
    text: str,
    *,
    limit: int = 20,
    form_type: str | None = None,
    cik: int | None = None,
) -> list[SearchHit]:
    """All words must match. If a plain query (no search syntax) matches nothing that way, any
    word may match, and filings matching more words rank higher: "insider selling" finds insider
    trades whose summaries say "sold"."""
    strict = sa.func.websearch_to_tsquery("english", text)
    hits = _keyword_hits(session, strict, limit=limit, form_type=form_type, cik=cik)
    if hits or not _is_plain(text) or len(text.split()) < 2:
        return hits
    relaxed = sa.cast(sa.func.replace(sa.cast(strict, sa.Text), "&", "|"), TSQUERY)
    return _keyword_hits(session, relaxed, limit=limit, form_type=form_type, cik=cik)


def _keyword_hits(
    session: Session,
    tsquery: sa.ColumnElement[str],
    *,
    limit: int,
    form_type: str | None,
    cik: int | None,
) -> list[SearchHit]:
    rank = sa.func.ts_rank_cd(FilingSearch.tsv, tsquery).label("rank")
    query = (
        sa.select(FilingSearch.accession_no, rank)
        .join(Filing, Filing.accession_no == FilingSearch.accession_no)
        .where(FilingSearch.tsv.bool_op("@@")(tsquery), *_filters(form_type=form_type, cik=cik))
        .order_by(rank.desc(), Filing.filed_at.desc(), FilingSearch.accession_no)
        .limit(limit)
    )
    rows = session.execute(query).all()
    return [SearchHit(accession_no, float(score)) for accession_no, score in rows]


def semantic_search(
    session: Session,
    embedding: Sequence[float],
    *,
    model: str,
    limit: int = 20,
    form_type: str | None = None,
    cik: int | None = None,
) -> list[SearchHit]:
    distance = FilingEmbedding.embedding.cosine_distance(list(embedding)).label("distance")
    query = (
        sa.select(FilingEmbedding.accession_no, distance)
        .join(Filing, Filing.accession_no == FilingEmbedding.accession_no)
        .where(FilingEmbedding.model == model, *_filters(form_type=form_type, cik=cik))
        .order_by(distance, FilingEmbedding.accession_no)
        .limit(limit)
    )
    rows = session.execute(query).all()
    return [SearchHit(accession_no, 1.0 - float(d)) for accession_no, d in rows]


def reciprocal_rank_fusion(*rankings: Sequence[SearchHit], k: int = RRF_K) -> list[SearchHit]:
    """Merge rankings: each list adds 1 / (k + rank) for every result it contains."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.accession_no] = scores.get(hit.accession_no, 0.0) + 1.0 / (k + rank)
    return [SearchHit(a, s) for a, s in sorted(scores.items(), key=lambda item: -item[1])]


def hybrid_search(
    session: Session,
    text: str,
    embedding: Sequence[float],
    *,
    model: str,
    limit: int = 20,
    form_type: str | None = None,
    cik: int | None = None,
) -> list[SearchHit]:
    """The top HYBRID_CANDIDATES of keyword and of semantic search, merged by reciprocal rank
    fusion. `limit` only cuts the merged list, so a page never changes the order."""
    keyword = keyword_search(session, text, limit=HYBRID_CANDIDATES, form_type=form_type, cik=cik)
    semantic = semantic_search(
        session, embedding, model=model, limit=HYBRID_CANDIDATES, form_type=form_type, cik=cik
    )
    return reciprocal_rank_fusion(keyword, semantic)[:limit]


def rebuild_index(sessions: sessionmaker[Session], embedder: Embedder | None) -> tuple[int, int]:
    """Rebuild every filing's search document from its current summary, then embed the ones that
    have no embedding for the configured model. Idempotent. Returns (documents, embedded)."""
    newer = aliased(Summary)
    latest = (
        sa.select(newer.id)
        .where(newer.accession_no == Filing.accession_no)
        .order_by(newer.created_at.desc(), newer.id.desc())
        .limit(1)
        .correlate(Filing)
        .scalar_subquery()
    )
    with sessions.begin() as session:
        rows = session.execute(
            sa.select(Filing.accession_no, Filing.form_type, Company.name, Company.ticker, Summary)
            .join(Company, Company.cik == Filing.cik)
            .join(Summary, Summary.id == latest)
        ).all()
        for accession_no, form_type, name, ticker, summary in rows:
            document = build_document(
                FilingSummary.model_validate(summary.summary),
                company_name=name,
                ticker=ticker,
                form_type=form_type,
            )
            upsert_document(session, accession_no, summary.id, document)
    if embedder is None:
        return len(rows), 0
    return len(rows), embed_missing(sessions, embedder)


def embed_missing(sessions: sessionmaker[Session], embedder: Embedder, batch: int = 32) -> int:
    """Embed search documents that have no embedding for the embedder's model."""
    with sessions() as session:
        missing = (
            session.execute(
                sa.select(FilingSearch)
                .outerjoin(
                    FilingEmbedding,
                    (FilingEmbedding.accession_no == FilingSearch.accession_no)
                    & (FilingEmbedding.model == embedder.model),
                )
                .where(FilingEmbedding.accession_no.is_(None))
                .order_by(FilingSearch.accession_no)
            )
            .scalars()
            .all()
        )
        documents = [
            (d.accession_no, SearchDocument(d.title, d.company, d.body).text()) for d in missing
        ]
    embedded = 0
    for start in range(0, len(documents), batch):
        chunk = documents[start : start + batch]
        try:
            vectors = embedder.embed_documents([text for _, text in chunk])
        except EmbeddingError:
            logger.exception(
                "embedding failed; %d documents remain unembedded", len(documents) - embedded
            )
            break
        with sessions.begin() as session:
            for (accession_no, _), vector in zip(chunk, vectors, strict=True):
                upsert_embedding(session, accession_no, embedder.model, vector)
        embedded += len(chunk)
    return embedded
