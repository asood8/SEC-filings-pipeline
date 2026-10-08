"""Search over filings: one document per filing, built from its current summary.

- Keyword search uses Postgres full-text search: a weighted `tsvector` (headline, company, and
  ticker above body and keywords) queried with `websearch_to_tsquery`, so quotes, OR, and -term
  work as people expect.
  - Keywords are other words for what the text says: "CFO" for "Chief Financial Officer" and the
    other way round, "purchase" for "bought". They are searched, never embedded or shown.
  - A ticker is indexed apart from the words, in capitals, so "costs" doesn't find Costco (COST).
    A query word typed in capitals, or one that no filing has as a word, also matches tickers:
    "COST" and "aapl" find those companies' filings.
- Semantic search compares a query embedding with document embeddings (pgvector, cosine).
- Hybrid search merges both rankings with reciprocal rank fusion, which needs no score
  calibration between the two: each list contributes 1 / (k + rank), except that a Form 4's
  semantic rank counts half (SEMANTIC_FORM_WEIGHTS).

`filings-search-eval` (search_eval.py) measures ranking changes on a labeled query set.
"""

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, TSQUERY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from filings_common import names
from filings_common.db import Company, Filing, FilingEmbedding, FilingSearch, Summary
from filings_common.embeddings import Embedder, EmbeddingError
from filings_common.summaries import FilingSummary

logger = logging.getLogger(__name__)

RRF_K = 60
# How many candidates each method contributes before hybrid fusion.
HYBRID_CANDIDATES = 50
# In hybrid search, a semantic match of these forms adds only this share of 1 / (k + rank).
# Form 4 documents come from one template and are over 90% of the filings, so they fill the
# semantic top 50 of almost any query, and each one that also matches a keyword collects both
# contributions, ahead of the 8-K being looked for. Chosen with filings-search-eval (ADR-0011).
SEMANTIC_FORM_WEIGHTS: Mapping[str, float] = {"4": 0.5}
# Words people search with that summaries don't necessarily contain.
FORM_LABELS = {
    "4": "Form 4 insider trading report",
    "8-K": "Form 8-K current report",
}
# Abbreviations and what they stand for. A document that has one form and not the other gets the
# other as keywords: "CFO" finds "Chief Financial Officer", and "chief financial officer" finds
# "EVP & CFO". Only unambiguous ones: CIO (information or investment officer?) and CLO (legal
# officer or loan obligation?) are left out. Changing these needs a `filings-index` rebuild.
ABBREVIATIONS: Mapping[str, str] = {
    "CEO": "chief executive officer",
    "CFO": "chief financial officer",
    "COO": "chief operating officer",
    "CTO": "chief technology officer",
    "CAO": "chief accounting officer",
    "EVP": "executive vice president",
    "SVP": "senior vice president",
    "VP": "vice president",
    "RSU": "restricted stock unit",
    "ESPP": "employee stock purchase plan",
    "EPS": "earnings per share",
    "IPO": "initial public offering",
}
# Irregular verb forms, which stemming doesn't relate to the words people search with, and those
# words. One way only: the Form 4 template writes "bought" for an open-market purchase (code P)
# and "sold" for a sale (S), and "insider purchase" should find them.
WORD_FORMS: Mapping[str, str] = {
    "bought": "buy purchase",
    "sold": "sell sale",
}
# Other words for what a word means here, also one way. The Form 4 template writes stock trades
# as "shares" ("gave 438,000 NVDA shares as a gift") and never "stock", so "stock gift" found one
# of seven gifts. Measured with filings-search-eval (ADR-0011): its recall@10 went from 0.14 to
# 1.00 (keyword) and 0.43 to 1.00 (hybrid), and no labeled query got worse.
RELATED_WORDS: Mapping[str, str] = {
    "shares": "stock",
}


def _pattern(phrase: str) -> re.Pattern[str]:
    """Matches the phrase as whole words, with an optional plural "s" and any spacing or hyphens
    between its words. In any case, except an abbreviation: "CAO" is not the name "Cao"."""
    words = r"[\s-]+".join(re.escape(word) for word in phrase.split())
    flags = 0 if phrase.isupper() else re.IGNORECASE
    return re.compile(rf"(?<![\w-]){words}s?(?![\w-])", flags)


# (what a text may say, the alias it then gets)
_ALIASES: list[tuple[re.Pattern[str], str]] = [
    *((_pattern(long), short) for short, long in ABBREVIATIONS.items()),
    *((_pattern(short), long) for short, long in ABBREVIATIONS.items()),
    *(
        (re.compile(rf"\b{word}\b", re.IGNORECASE), words)
        for word, words in [*WORD_FORMS.items(), *RELATED_WORDS.items()]
    ),
]
# A query word shaped like a ticker (compared in capitals): up to 5 letters and digits, starting
# with a letter, maybe with a class or series after "." or "-" (BRK-B, BRK.A).
_TICKER_WORD = re.compile(r"[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z0-9]{1,3})?")


@dataclass(frozen=True)
class SearchDocument:
    title: str
    company: str  # the company's name, as names.company_name shows it
    body: str
    ticker: str | None = None
    # Searched, not embedded: other words for what the text says (keywords_for).
    keywords: str = ""

    def text(self) -> str:
        """The text that gets embedded: the headline, the company's name and ticker, the body."""
        company = f"{self.company} {self.ticker}" if self.ticker else self.company
        return f"{self.title}\n{company}\n{self.body}"


@dataclass(frozen=True)
class SearchHit:
    accession_no: str
    score: float
    form_type: str | None = None


def keywords_for(text: str) -> str:
    """Other words for what the text says, one per line: what an abbreviation stands for, or the
    reverse, when the text has only one of the two (ABBREVIATIONS), the plain words for an
    irregular verb form (WORD_FORMS), and related words the text doesn't have (RELATED_WORDS)."""
    found = [alias for pattern, alias in _ALIASES if pattern.search(text)]
    return "\n".join(dict.fromkeys(a for a in found if not _pattern(a).search(text)))


def build_document(
    summary: FilingSummary, *, company_name: str, ticker: str | None, form_type: str
) -> SearchDocument:
    """The search document of a summary. The company's name may be EDGAR's ("CHEVRON CORP") or
    already shown the way names.company_name shows it ("Chevron Corp"): the document gets the
    latter either way, so the summarizer and `filings-index` write the same text."""
    label = FORM_LABELS.get(form_type, f"Form {form_type}")
    body = "\n".join([summary.summary, *summary.key_points, label])
    return SearchDocument(
        title=summary.headline,
        company=names.company_name(company_name),
        body=body,
        ticker=ticker or None,
        keywords=keywords_for(f"{summary.headline}\n{body}"),
    )


def upsert_document(
    session: Session, accession_no: str, summary_id: int, document: SearchDocument
) -> None:
    values = {
        "summary_id": summary_id,
        "title": document.title,
        "company": document.company,
        "ticker": document.ticker,
        "body": document.body,
        "keywords": document.keywords,
    }
    stmt = pg_insert(FilingSearch).values(accession_no=accession_no, **values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FilingSearch.accession_no],
            set_={**values, "updated_at": sa.func.now()},
        )
    )


def text_sha256(text: str) -> str:
    """The hash stored with an embedding of `text` (FilingEmbedding.text_sha256)."""
    return hashlib.sha256(text.encode()).hexdigest()


def upsert_embedding(
    session: Session, accession_no: str, model: str, embedding: Sequence[float], *, text: str
) -> None:
    """Store a filing's embedding for `model`, made from `text` (its SearchDocument.text())."""
    stmt = pg_insert(FilingEmbedding).values(
        accession_no=accession_no,
        model=model,
        embedding=list(embedding),
        text_sha256=text_sha256(text),
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FilingEmbedding.accession_no, FilingEmbedding.model],
            set_={
                "embedding": stmt.excluded.embedding,
                "text_sha256": stmt.excluded.text_sha256,
                "updated_at": sa.func.now(),
            },
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
    word may match, and filings matching more words rank higher: "credit facility pizza" still
    finds the credit facility. Some words also match tickers (_ticker_words)."""
    strict = sa.func.websearch_to_tsquery("english", text)
    for word in _ticker_words(session, text):
        # The query's 'cost' becomes ('cost' | 'COST') wherever it is, in a phrase or after a
        # minus. _TICKER_WORD allows no quotes or backslashes, so the ticker is one lexeme.
        as_word = sa.func.websearch_to_tsquery("english", word)
        as_ticker = sa.cast(sa.literal(f"'{word.upper()}'"), TSQUERY)
        strict = sa.func.ts_rewrite(strict, as_word, as_word.op("||")(as_ticker))
    hits = _keyword_hits(session, strict, limit=limit, form_type=form_type, cik=cik)
    if hits or not _is_plain(text) or len(text.split()) < 2:
        return hits
    relaxed = sa.cast(sa.func.replace(sa.cast(strict, sa.Text), "&", "|"), TSQUERY)
    return _keyword_hits(session, relaxed, limit=limit, form_type=form_type, cik=cik)


def _ticker_words(session: Session, text: str) -> list[str]:
    """The query's words that also match tickers: those shaped like a ticker and typed in
    capitals (COST, BRK-B), or in any case if no filing has them as a word ("aapl" finds Apple's
    filings; "cost" doesn't find Costco's). "costs" is never a ticker.

    Tickers are indexed in capitals and words in lowercase, so only these words match tickers. A
    ticker that is also a stop word (IT, ALL) can't be searched."""
    words = {word.strip('"()[]{},;:!?').lstrip("-") for word in text.split()}
    shaped = sorted(w for w in words if _TICKER_WORD.fullmatch(w.upper()) and w.upper() != "OR")
    other_case = [w for w in shaped if not w.isupper()]
    if not other_case:
        return shaped
    # One query for all of them: a word's tsquery must be constant in the search itself, or
    # Postgres would compute it for every row.
    word = sa.func.unnest(sa.literal(other_case, ARRAY(sa.Text))).column_valued("word")
    as_word = sa.func.websearch_to_tsquery("english", word)
    not_words = session.scalars(
        sa.select(word).where(
            sa.func.numnode(as_word) > 0,  # stop words: nothing to rewrite
            ~sa.exists().where(FilingSearch.tsv.bool_op("@@")(as_word)),
        )
    ).all()
    return [w for w in shaped if w.isupper() or w in not_words]


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
        sa.select(FilingSearch.accession_no, rank, Filing.form_type)
        .join(Filing, Filing.accession_no == FilingSearch.accession_no)
        .where(FilingSearch.tsv.bool_op("@@")(tsquery), *_filters(form_type=form_type, cik=cik))
        .order_by(rank.desc(), Filing.filed_at.desc(), FilingSearch.accession_no)
        .limit(limit)
    )
    rows = session.execute(query).all()
    return [SearchHit(accession_no, float(score), form) for accession_no, score, form in rows]


def semantic_search(
    session: Session,
    embedding: Sequence[float],
    *,
    model: str,
    limit: int = 20,
    form_type: str | None = None,
    cik: int | None = None,
) -> list[SearchHit]:
    # The HNSW index scan stops at its hnsw.ef_search (40) nearest vectors, and the filters apply
    # after it: without iterative scans, a form or company filter returns only those of the 40
    # that pass (none for 8-Ks once Form 4s fill them), and hybrid search gets 40 candidates, not
    # 50. Iterative scans go on, nearest first, until the limit is met or hnsw.max_scan_tuples
    # (20,000) are visited. For the transaction only; small or selective searches don't use the
    # index at all (the planner computes those distances exactly).
    session.execute(sa.text("SET LOCAL hnsw.iterative_scan = strict_order"))
    distance = FilingEmbedding.embedding.cosine_distance(list(embedding)).label("distance")
    query = (
        sa.select(FilingEmbedding.accession_no, distance, Filing.form_type)
        .join(Filing, Filing.accession_no == FilingEmbedding.accession_no)
        .where(FilingEmbedding.model == model, *_filters(form_type=form_type, cik=cik))
        .order_by(distance, FilingEmbedding.accession_no)
        .limit(limit)
    )
    rows = session.execute(query).all()
    return [SearchHit(accession_no, 1.0 - float(d), form) for accession_no, d, form in rows]


def reciprocal_rank_fusion(
    *rankings: Sequence[SearchHit],
    k: int = RRF_K,
    form_weights: Sequence[Mapping[str, float]] = (),
) -> list[SearchHit]:
    """Merge rankings: each list adds 1 / (k + rank) for every result it contains. The i-th
    mapping in `form_weights`, if any, scales the i-th list's contributions by form type."""
    scores: dict[str, float] = {}
    forms: dict[str, str | None] = {}
    for i, ranking in enumerate(rankings):
        weights = form_weights[i] if i < len(form_weights) else {}
        for rank, hit in enumerate(ranking, start=1):
            weight = weights.get(hit.form_type, 1.0) if hit.form_type is not None else 1.0
            scores[hit.accession_no] = scores.get(hit.accession_no, 0.0) + weight / (k + rank)
            forms.setdefault(hit.accession_no, hit.form_type)
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    return [SearchHit(a, s, forms[a]) for a, s in ranked]


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
    fusion. `limit` only cuts the merged list, so a page never changes the order.

    SEMANTIC_FORM_WEIGHTS apply when forms compete: with a `form_type` filter, they would only
    shift the balance between keyword and semantic matches."""
    keyword = keyword_search(session, text, limit=HYBRID_CANDIDATES, form_type=form_type, cik=cik)
    semantic = semantic_search(
        session, embedding, model=model, limit=HYBRID_CANDIDATES, form_type=form_type, cik=cik
    )
    weights = [{}, SEMANTIC_FORM_WEIGHTS] if form_type is None else []
    return reciprocal_rank_fusion(keyword, semantic, form_weights=weights)[:limit]


def rebuild_index(sessions: sessionmaker[Session], embedder: Embedder | None) -> tuple[int, int]:
    """Rebuild every filing's search document from its current summary, then embed the ones whose
    embedding for the configured model is missing or was made from other text (embed_outdated).
    Idempotent. Returns (documents, embedded)."""
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
    return len(rows), embed_outdated(sessions, embedder)


def embed_outdated(sessions: sessionmaker[Session], embedder: Embedder, batch: int = 32) -> int:
    """Embed the search documents that have no embedding for the embedder's model, or whose
    embedding was made from other text (its stored hash differs): the document changed since it
    was embedded, for example rebuilt after a change to how documents are built, or rewritten
    while the embedder was down; or the vector predates the hash. A document whose text is
    unchanged is never embedded again. Returns how many were embedded."""
    query = (
        sa.select(
            FilingSearch.accession_no,
            FilingSearch.title,
            FilingSearch.company,
            FilingSearch.body,
            FilingSearch.ticker,
            FilingEmbedding.text_sha256,
        )
        .outerjoin(
            FilingEmbedding,
            (FilingEmbedding.accession_no == FilingSearch.accession_no)
            & (FilingEmbedding.model == embedder.model),
        )
        .order_by(FilingSearch.accession_no)
        .execution_options(yield_per=500)
    )
    documents = []
    with sessions() as session:
        # Hashed here rather than in SQL, so SearchDocument.text() stays the one definition of
        # the embedded text.
        for row in session.execute(query):
            text = SearchDocument(row.title, row.company, row.body, row.ticker).text()
            if row.text_sha256 != text_sha256(text):
                documents.append((row.accession_no, text))
    embedded = 0
    for start in range(0, len(documents), batch):
        chunk = documents[start : start + batch]
        try:
            vectors = embedder.embed_documents([text for _, text in chunk])
        except EmbeddingError:
            logger.exception(
                "embedding failed; %d documents remain to embed", len(documents) - embedded
            )
            break
        with sessions.begin() as session:
            for (accession_no, text), vector in zip(chunk, vectors, strict=True):
                upsert_embedding(session, accession_no, embedder.model, vector, text=text)
        embedded += len(chunk)
    return embedded
