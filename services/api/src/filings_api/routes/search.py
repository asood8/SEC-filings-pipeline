import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from filings_api.embedding_cache import QueryEmbeddingCache
from filings_api.queries import CikFilter, EmbedderDep, SessionDep, filing_select, to_filing_out
from filings_api.schemas import SearchResponse, SearchResultOut
from filings_common.db import Filing
from filings_common.embeddings import Embedder, EmbeddingError
from filings_common.search import SearchHit, hybrid_search, keyword_search, semantic_search

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


def get_query_embeddings(request: Request) -> QueryEmbeddingCache | None:
    cache: QueryEmbeddingCache | None = request.app.state.query_embeddings
    return cache


QueryEmbeddingsDep = Annotated[QueryEmbeddingCache | None, Depends(get_query_embeddings)]


def query_vector(
    embedder: Embedder, cache: QueryEmbeddingCache | None, query: str
) -> list[float] | None:
    """The query's embedding, from the cache or the model. None if the model fails or is too slow
    (its query timeout): the caller falls back to keywords."""
    if cache is not None and (cached := cache.get(embedder.model, query)) is not None:
        return cached
    try:
        vector = embedder.embed_query(query)
    except EmbeddingError as exc:
        # No traceback: a busy model makes this routine, and the message says what happened.
        logger.warning("query embedding failed; falling back to keyword search: %s", exc)
        return None
    if cache is not None:
        cache.put(embedder.model, query, vector)
    return vector


@router.get("/search", summary="Search filings by keyword, meaning, or both")
def search(
    session: SessionDep,
    embedder: EmbedderDep,
    query_embeddings: QueryEmbeddingsDep,
    response: Response,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid",
    form_type: str | None = None,
    cik: CikFilter = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchResponse:
    """Keyword search understands quotes, `OR`, and `-word`. Semantic search matches meaning
    ("leadership change" finds CEO appointments). Hybrid, the default, combines both. Without a
    working embedding model, semantic and hybrid fall back to keywords (`degraded: true`)."""
    vector = None
    if mode != "keyword" and embedder is not None:
        vector = query_vector(embedder, query_embeddings, q)
        if vector is None:
            # A failing model is usually a busy or restarting one: the next request may get full
            # results, so no cache may keep serving this fallback.
            response.headers["Cache-Control"] = "no-store"

    hits: list[SearchHit]
    if vector is None or embedder is None:
        used: Literal["keyword", "semantic", "hybrid"] = "keyword"
        hits = keyword_search(session, q, limit=limit, form_type=form_type, cik=cik)
    elif mode == "semantic":
        used = "semantic"
        hits = semantic_search(
            session, vector, model=embedder.model, limit=limit, form_type=form_type, cik=cik
        )
    else:
        used = "hybrid"
        hits = hybrid_search(
            session, q, vector, model=embedder.model, limit=limit, form_type=form_type, cik=cik
        )

    rows = session.execute(
        filing_select().where(Filing.accession_no.in_([h.accession_no for h in hits]))
    ).all()
    filings = {row[0].accession_no: to_filing_out(*row) for row in rows}
    return SearchResponse(
        query=q,
        mode=used,
        degraded=used != mode,
        items=[
            SearchResultOut(**filings[h.accession_no].model_dump(), score=h.score)
            for h in hits
            if h.accession_no in filings
        ],
    )
