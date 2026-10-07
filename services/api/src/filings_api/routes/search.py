import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from filings_api.queries import EmbedderDep, SessionDep, filing_select, to_filing_out
from filings_api.schemas import SearchResponse, SearchResultOut
from filings_common.db import Filing
from filings_common.embeddings import EmbeddingError
from filings_common.search import (
    SearchHit,
    keyword_search,
    reciprocal_rank_fusion,
    semantic_search,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])

# How many candidates each method contributes before hybrid fusion.
HYBRID_CANDIDATES = 50


@router.get("/search", summary="Search filings by keyword, meaning, or both")
def search(
    session: SessionDep,
    embedder: EmbedderDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid",
    form_type: str | None = None,
    cik: int | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchResponse:
    """Keyword search understands quotes, `OR`, and `-word`. Semantic search matches meaning
    ("leadership change" finds CEO appointments). Hybrid, the default, combines both. Without a
    working embedding model, semantic and hybrid fall back to keywords (`degraded: true`)."""
    vector = None
    if mode != "keyword" and embedder is not None:
        try:
            vector = embedder.embed_query(q)
        except EmbeddingError:
            logger.warning("query embedding failed; falling back to keyword search", exc_info=True)

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
        keyword = keyword_search(session, q, limit=HYBRID_CANDIDATES, form_type=form_type, cik=cik)
        semantic = semantic_search(
            session,
            vector,
            model=embedder.model,
            limit=HYBRID_CANDIDATES,
            form_type=form_type,
            cik=cik,
        )
        hits = reciprocal_rank_fusion(keyword, semantic)[:limit]

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
