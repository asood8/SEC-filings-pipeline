import hashlib
import math
import re
from collections.abc import Sequence
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.db import (
    EMBEDDING_DIMENSIONS,
    Company,
    Filing,
    FilingEmbedding,
    FilingSearch,
    Summary,
    session_factory,
)
from filings_common.embeddings import EmbeddingError
from filings_common.search import (
    SearchHit,
    build_document,
    embed_missing,
    keyword_search,
    rebuild_index,
    reciprocal_rank_fusion,
    semantic_search,
)
from filings_common.summaries import FilingSummary


class WordHashEmbedder:
    """Deterministic stand-in for an embedding model: texts sharing words get similar vectors."""

    model = "word-hash"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % EMBEDDING_DIMENSIONS] += 1
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingError("embedding server down")
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


# (accession, cik, form, filed, headline, summary)
FILINGS = [
    (
        "0000000001-26-000001",
        1,
        "8-K",
        date(2026, 9, 1),
        "Acme names Jane Doe chief executive officer",
        "Acme Corp appointed Jane Doe as CEO, succeeding John Roe, who retires.",
    ),
    (
        "0000000001-26-000002",
        1,
        "8-K",
        date(2026, 9, 2),
        "Acme enters $2 billion credit facility",
        "Acme Corp signed a senior unsecured revolving credit facility of $2 billion.",
    ),
    (
        "0000000002-26-000003",
        2,
        "4",
        date(2026, 9, 3),
        "Bob Smith sold 1,000 GLOB shares",
        "Bob Smith (CFO) sold 1,000 Globex shares on the open market.",
    ),
    (
        "0000000002-26-000004",
        2,
        "8-K",
        date(2026, 9, 4),
        "Globex reports record quarterly revenue",
        "Globex Inc reported record revenue and raised its full-year outlook.",
    ),
]


@pytest.fixture
def indexed(clean_db: Engine) -> Engine:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        session.add_all(
            [
                Company(cik=1, ticker="ACME", name="Acme Corp", on_watchlist=True),
                Company(cik=2, ticker="GLOB", name="Globex Inc", on_watchlist=True),
            ]
        )
        session.flush()
        for accession_no, cik, form, filed, headline, summary in FILINGS:
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=cik,
                    form_type=form,
                    filed_at=filed,
                    primary_doc_url="https://www.sec.gov/x",
                    raw_object_key=f"filings/{accession_no}",
                )
            )
            session.flush()
            session.add(
                Summary(
                    accession_no=accession_no,
                    provider="test",
                    model="test",
                    prompt_version=1,
                    summary={"headline": headline, "summary": summary, "key_points": []},
                )
            )
    assert rebuild_index(sessions, WordHashEmbedder()) == (4, 4)
    return clean_db


def accessions(hits: Sequence[SearchHit]) -> list[str]:
    return [h.accession_no for h in hits]


pytestmark = pytest.mark.integration


def test_build_document_adds_company_and_form_label() -> None:
    summary = FilingSummary(headline="H", summary="S", key_points=["K1", "K2"])
    document = build_document(summary, company_name="Acme Corp", ticker="ACME", form_type="4")
    assert (document.title, document.company) == ("H", "Acme Corp ACME")
    assert document.body == "S\nK1\nK2\nForm 4 insider trading report"


def test_keyword_search_ranks_headline_matches_first(indexed: Engine) -> None:
    with session_factory(indexed)() as session:
        assert accessions(keyword_search(session, "chief executive"))[0] == FILINGS[0][0]
        assert accessions(keyword_search(session, "insider")) == [FILINGS[2][0]]  # form label
        assert set(accessions(keyword_search(session, "GLOB"))) == {FILINGS[2][0], FILINGS[3][0]}


def test_keyword_search_supports_web_search_syntax(indexed: Engine) -> None:
    with session_factory(indexed)() as session:
        assert accessions(keyword_search(session, '"credit facility"')) == [FILINGS[1][0]]
        either = set(accessions(keyword_search(session, "revenue OR facility")))
        assert either == {FILINGS[1][0], FILINGS[3][0]}
        assert FILINGS[0][0] not in accessions(keyword_search(session, "Acme -executive"))


def test_plain_queries_relax_to_any_word_when_nothing_matches_all(indexed: Engine) -> None:
    with session_factory(indexed)() as session:
        # No filing contains both "insider" and "selling" ("sold" doesn't stem to "sell").
        assert accessions(keyword_search(session, "insider selling")) == [FILINGS[2][0]]
        # A filing matching more of the words ranks first.
        assert accessions(keyword_search(session, "credit facility pizza"))[0] == FILINGS[1][0]
        # Explicit syntax is never relaxed: these keep their strict meaning.
        assert keyword_search(session, '"insider selling"') == []
        assert keyword_search(session, "insider -Bob") == []


def test_filters_by_form_and_company(indexed: Engine) -> None:
    with session_factory(indexed)() as session:
        assert accessions(keyword_search(session, "Acme OR Globex", form_type="4")) == [
            FILINGS[2][0]
        ]
        assert set(accessions(keyword_search(session, "Acme OR Globex", cik=1))) == {
            FILINGS[0][0],
            FILINGS[1][0],
        }


def test_semantic_search_orders_by_similarity(indexed: Engine) -> None:
    query = WordHashEmbedder().embed_query("record revenue outlook")
    with session_factory(indexed)() as session:
        hits = semantic_search(session, query, model="word-hash", limit=4)
        assert hits[0].accession_no == FILINGS[3][0]
        assert hits[0].score > hits[-1].score
        assert semantic_search(session, query, model="another-model") == []


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    keyword = [SearchHit("a", 9.0), SearchHit("b", 5.0)]
    semantic = [SearchHit("b", 0.9), SearchHit("c", 0.8)]
    assert accessions(reciprocal_rank_fusion(keyword, semantic)) == ["b", "a", "c"]


def test_rebuild_is_idempotent_and_embeds_only_missing(indexed: Engine) -> None:
    sessions = session_factory(indexed)
    assert rebuild_index(sessions, WordHashEmbedder()) == (4, 0)
    with sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FilingSearch)) == 4
        assert session.scalar(sa.select(sa.func.count()).select_from(FilingEmbedding)) == 4


def test_embedding_failure_leaves_documents_searchable_by_keyword(indexed: Engine) -> None:
    sessions = session_factory(indexed)
    with sessions.begin() as session:
        session.execute(sa.delete(FilingEmbedding))
    assert embed_missing(sessions, WordHashEmbedder(fail=True)) == 0
    with sessions() as session:
        assert keyword_search(session, "Acme")
