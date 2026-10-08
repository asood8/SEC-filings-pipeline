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
    HYBRID_CANDIDATES,
    SEMANTIC_FORM_WEIGHTS,
    SearchDocument,
    SearchHit,
    build_document,
    embed_missing,
    hybrid_search,
    keyword_search,
    keywords_for,
    rebuild_index,
    reciprocal_rank_fusion,
    semantic_search,
    upsert_document,
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


def add_filings(
    engine: Engine,
    companies: Sequence[tuple[int, str, str]],
    filings: Sequence[tuple[str, int, str, date, str, str]],
) -> None:
    """Companies (cik, ticker, name), and filings with a summary each, as in FILINGS."""
    with session_factory(engine).begin() as session:
        session.add_all(
            Company(cik=cik, ticker=ticker, name=name, on_watchlist=True)
            for cik, ticker, name in companies
        )
        session.flush()
        for accession_no, cik, form, filed, headline, summary in filings:
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


@pytest.fixture
def indexed(clean_db: Engine) -> Engine:
    add_filings(clean_db, [(1, "ACME", "Acme Corp"), (2, "GLOB", "Globex Inc")], FILINGS)
    assert rebuild_index(session_factory(clean_db), WordHashEmbedder()) == (4, 4)
    return clean_db


# Filings whose words collide with a ticker or need other words to be found.
COST_SALE = "0000000003-26-000001"  # Costco's ticker is COST
CUT_COSTS = "0000000001-26-000002"
BERKSHIRE_BUY = "0000000004-26-000003"
WORDED = [
    (
        COST_SALE,
        3,
        "4",
        date(2026, 9, 1),
        "Jane Roe sold 100 COST shares for $90.0K (avg $900.00/share)",
        "Jane Roe (EVP & CFO) reported 1 transaction in Costco Wholesale Corp (COST) securities. "
        "Jane Roe sold 100 COST shares for $90.0K (avg $900.00/share).",
    ),
    (
        CUT_COSTS,
        1,
        "8-K",
        date(2026, 9, 2),
        "Acme plans to cut costs",
        "Acme Corp will close two plants. Its Chief Financial Officer, John Roe, retires.",
    ),
    (
        BERKSHIRE_BUY,
        4,
        "4",
        date(2026, 9, 3),
        "Berkshire Hathaway Inc bought 1,000 LEN shares",
        "Berkshire Hathaway Inc (BRK.B) bought 1,000 LEN shares on the open market. BRKXB.",
    ),
]


@pytest.fixture
def worded(clean_db: Engine) -> Engine:
    companies = [
        (1, "ACME", "Acme Corp"),
        (3, "COST", "Costco Wholesale Corp"),
        (4, "BRK.B", "Berkshire Hathaway Inc"),
    ]
    add_filings(clean_db, companies, WORDED)
    assert rebuild_index(session_factory(clean_db), None) == (3, 0)
    return clean_db


def tsv(engine: Engine, accession_no: str) -> str:
    with session_factory(engine)() as session:
        return session.scalars(
            sa.select(FilingSearch.tsv).where(FilingSearch.accession_no == accession_no)
        ).one()


def accessions(hits: Sequence[SearchHit]) -> list[str]:
    return [h.accession_no for h in hits]


pytestmark = pytest.mark.integration


def test_build_document_adds_company_and_form_label() -> None:
    summary = FilingSummary(headline="H", summary="S", key_points=["K1", "K2"])
    document = build_document(summary, company_name="Acme Corp", ticker="ACME", form_type="4")
    assert (document.title, document.company, document.ticker) == ("H", "Acme Corp", "ACME")
    assert document.body == "S\nK1\nK2\nForm 4 insider trading report"
    assert document.keywords == ""
    # The embedded text is what it was before the ticker had a field of its own, so existing
    # embeddings still match their documents.
    assert document.text() == "H\nAcme Corp ACME\nS\nK1\nK2\nForm 4 insider trading report"
    no_ticker = build_document(summary, company_name="Acme Corp", ticker="", form_type="8-K")
    assert no_ticker.ticker is None
    assert no_ticker.text() == "H\nAcme Corp\nS\nK1\nK2\nForm 8-K current report"


def test_build_document_adds_other_words_as_keywords_only() -> None:
    summary = FilingSummary(
        headline="Jane Roe bought 100 ACME shares",
        summary="Jane Roe (EVP & CFO) reported 1 transaction.",
        key_points=["Bought 100 ACME shares"],
    )
    document = build_document(summary, company_name="Acme Corp", ticker="ACME", form_type="4")
    assert document.keywords.splitlines() == [
        "chief financial officer",
        "executive vice president",
        "buy purchase",
    ]
    assert "purchase" not in document.text()  # searchable, but not embedded


def test_keywords_spell_out_abbreviations_and_abbreviate_titles() -> None:
    assert keywords_for("Chevron elects a new Chief Financial Officer") == "CFO"
    assert keywords_for("Jane Roe (SVP, CFO)") == "chief financial officer\nsenior vice president"
    # Hyphens, line breaks, plurals, any case for the long form.
    assert keywords_for("granted restricted stock\nunits") == "RSU"
    assert keywords_for("Two RSUs vested") == "restricted stock unit"
    assert keywords_for("Senior Vice-President") == "SVP\nVP"
    # A text with both forms needs neither.
    assert keywords_for("Chief Executive Officer (CEO)") == ""
    # Abbreviations count only in capitals and as whole words: "Cao" is a name.
    assert keywords_for("Wei Cao met the CFOs; xCEO") == "chief financial officer"


def test_keywords_add_plain_words_for_irregular_verbs() -> None:
    assert keywords_for("Jane Roe bought 100 ACME shares") == "buy purchase"
    assert keywords_for("John Doe SOLD 50 shares and sold 20 more") == "sell sale"
    assert keywords_for("Jane Roe received 100 ACME shares as a grant or award") == ""


def test_keyword_search_ranks_headline_matches_first(indexed: Engine) -> None:
    with session_factory(indexed)() as session:
        assert accessions(keyword_search(session, "chief executive"))[0] == FILINGS[0][0]
        insider = keyword_search(session, "insider")  # matches the form label
        assert [(h.accession_no, h.form_type) for h in insider] == [(FILINGS[2][0], "4")]
        assert set(accessions(keyword_search(session, "GLOB"))) == {FILINGS[2][0], FILINGS[3][0]}


def test_keyword_search_supports_web_search_syntax(indexed: Engine) -> None:
    with session_factory(indexed)() as session:
        assert accessions(keyword_search(session, '"credit facility"')) == [FILINGS[1][0]]
        either = set(accessions(keyword_search(session, "revenue OR facility")))
        assert either == {FILINGS[1][0], FILINGS[3][0]}
        assert FILINGS[0][0] not in accessions(keyword_search(session, "Acme -executive"))


def test_plain_queries_relax_to_any_word_when_nothing_matches_all(indexed: Engine) -> None:
    with session_factory(indexed)() as session:
        # No filing has both words.
        relaxed = set(accessions(keyword_search(session, "insider revenue")))
        assert relaxed == {FILINGS[2][0], FILINGS[3][0]}
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


def test_a_ticker_that_is_a_word_stops_matching_the_word(worded: Engine) -> None:
    with session_factory(worded)() as session:
        # The ticker isn't indexed as a word, so the word's forms don't find Costco...
        assert accessions(keyword_search(session, "costs")) == [CUT_COSTS]
        assert accessions(keyword_search(session, "restructuring to cut costs")) == [CUT_COSTS]
        # ...nor does the word itself, which filings have as a word.
        assert accessions(keyword_search(session, "cost")) == [CUT_COSTS]
        # Typed in capitals, it is the ticker as well as the word, wherever it is in the query.
        assert set(accessions(keyword_search(session, "COST"))) == {COST_SALE, CUT_COSTS}
        assert accessions(keyword_search(session, "COST shares")) == [COST_SALE]
        assert accessions(keyword_search(session, "shares -COST")) == [BERKSHIRE_BUY]


def test_a_ticker_no_filing_has_as_a_word_matches_in_any_case(worded: Engine) -> None:
    with session_factory(worded)() as session:
        for query in ("BRK.B", "brk.b", "Brk.B shares"):
            assert accessions(keyword_search(session, query)) == [BERKSHIRE_BUY], query


def test_tickers_are_indexed_apart_from_the_words(worded: Engine) -> None:
    cost = tsv(worded, COST_SALE)
    assert "'COST':" in cost
    assert "'cost'" not in cost
    berkshire = tsv(worded, BERKSHIRE_BUY)
    assert "'BRK.B':" in berkshire
    assert "'brk.b'" not in berkshire
    # The ticker is escaped for the regular expression: its "." matches only itself.
    assert "'brkxb'" in berkshire


def test_any_ticker_text_makes_a_valid_lexeme(worded: Engine) -> None:
    ticker = "O'N\\X"
    with session_factory(worded).begin() as session:
        summary_id = session.scalars(
            sa.select(Summary.id).where(Summary.accession_no == COST_SALE)
        ).one()
        document = SearchDocument(f"{ticker} sold", "Odd Co", "body", ticker=ticker)
        upsert_document(session, COST_SALE, summary_id, document)
    assert tsv(worded, COST_SALE).startswith("'O''N\\\\X':")  # quotes and backslashes escaped


def test_abbreviations_and_what_they_stand_for_find_each_other(worded: Engine) -> None:
    with session_factory(worded)() as session:
        # One filing says "CFO", the other "Chief Financial Officer".
        both = {COST_SALE, CUT_COSTS}
        assert set(accessions(keyword_search(session, "CFO"))) == both
        assert set(accessions(keyword_search(session, "chief financial officer"))) == both
        assert accessions(keyword_search(session, "executive vice president")) == [COST_SALE]


def test_insider_trades_are_found_by_purchase_and_sale(worded: Engine) -> None:
    with session_factory(worded)() as session:
        # The Form 4 template says "bought" and "sold".
        assert accessions(keyword_search(session, "insider purchase")) == [BERKSHIRE_BUY]
        assert accessions(keyword_search(session, "insider buying")) == [BERKSHIRE_BUY]
        assert accessions(keyword_search(session, "insider sale")) == [COST_SALE]
        assert accessions(keyword_search(session, "insider selling")) == [COST_SALE]


def test_semantic_search_orders_by_similarity(indexed: Engine) -> None:
    query = WordHashEmbedder().embed_query("record revenue outlook")
    with session_factory(indexed)() as session:
        hits = semantic_search(session, query, model="word-hash", limit=4)
        assert hits[0].accession_no == FILINGS[3][0]
        assert hits[0].score > hits[-1].score
        assert {(h.accession_no, h.form_type) for h in hits} == {(f[0], f[2]) for f in FILINGS}
        assert semantic_search(session, query, model="another-model") == []


def test_semantic_search_through_the_index_fills_the_limit_despite_filters(
    clean_db: Engine,
) -> None:
    """The HNSW index scan stops at hnsw.ef_search candidates and filters apply afterwards, so a
    filter could leave fewer results than exist, or none (seen at volume: an 8-K filter returned
    nothing once Form 4s filled the 40 nearest). Iterative scans keep going."""
    form4s = [
        (f"0000000001-26-{n:06d}", 1, "4", date(2026, 9, 1), f"Insider {n} sold shares", "Sold.")
        for n in range(1, 31)
    ]
    form8ks = [
        (f"0000000001-26-{n:06d}", 1, "8-K", date(2026, 9, 2), f"Acme item {n}", "Board news.")
        for n in range(31, 36)
    ]
    add_filings(clean_db, [(1, "ACME", "Acme Corp")], form4s + form8ks)
    assert rebuild_index(session_factory(clean_db), WordHashEmbedder()) == (35, 35)
    query = WordHashEmbedder().embed_query("insider sold shares")
    with session_factory(clean_db)() as session:
        # Make this tiny table take the index path that big ones take, with few candidates.
        for setting in ("enable_seqscan = off", "enable_sort = off", "hnsw.ef_search = 10"):
            session.execute(sa.text(f"SET LOCAL {setting}"))
        plan = session.execute(
            sa.text(
                "EXPLAIN SELECT accession_no FROM filing_embeddings"
                " ORDER BY embedding <=> CAST(:q AS vector) LIMIT 20"
            ),
            {"q": str(query)},
        ).scalars()
        assert any("ix_filing_embeddings_embedding_hnsw" in line for line in plan)
        eight_ks = semantic_search(session, query, model="word-hash", limit=5, form_type="8-K")
        assert sorted(accessions(eight_ks)) == [f[0] for f in form8ks]
        assert len(semantic_search(session, query, model="word-hash", limit=20)) == 20


def test_embedded_text_includes_the_ticker(indexed: Engine) -> None:
    query = WordHashEmbedder().embed_query("GLOB")
    with session_factory(indexed)() as session:
        hits = semantic_search(session, query, model="word-hash", limit=4)
    # The Globex 8-K has the ticker only on its company line.
    assert {h.accession_no for h in hits[:2]} == {FILINGS[2][0], FILINGS[3][0]}
    assert hits[2].score == pytest.approx(0.0)


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    keyword = [SearchHit("a", 9.0), SearchHit("b", 5.0)]
    semantic = [SearchHit("b", 0.9), SearchHit("c", 0.8)]
    assert accessions(reciprocal_rank_fusion(keyword, semantic)) == ["b", "a", "c"]


def test_reciprocal_rank_fusion_weighs_a_list_by_form_type() -> None:
    keyword = [SearchHit("f4", 9.0, "4"), SearchHit("8k", 5.0, "8-K")]
    semantic = [SearchHit("8k", 0.9, "8-K"), SearchHit("f4", 0.8, "4")]
    # A tie without weights; with a Form 4's semantic rank at half weight, the 8-K wins.
    unweighted = reciprocal_rank_fusion(keyword, semantic)
    assert unweighted[0].score == unweighted[1].score
    fused = reciprocal_rank_fusion(keyword, semantic, form_weights=[{}, {"4": 0.5}])
    assert [(h.accession_no, h.form_type) for h in fused] == [("8k", "8-K"), ("f4", "4")]
    assert fused[1].score == pytest.approx(1 / 61 + 0.5 / 62)


def test_hybrid_search_halves_form4_semantic_ranks_unless_filtered_by_form(
    indexed: Engine,
) -> None:
    query = "Globex shares revenue"
    vector = WordHashEmbedder().embed_query(query)
    form4 = FILINGS[2][0]
    with session_factory(indexed)() as session:
        keyword = accessions(keyword_search(session, query, limit=HYBRID_CANDIDATES))
        semantic = accessions(
            semantic_search(session, vector, model="word-hash", limit=HYBRID_CANDIDATES)
        )
        hits = hybrid_search(session, query, vector, model="word-hash")
        score = {h.accession_no: h.score for h in hits}[form4]
        weight = SEMANTIC_FORM_WEIGHTS["4"]
        expected = 1 / (60 + keyword.index(form4) + 1) + weight / (60 + semantic.index(form4) + 1)
        assert score == pytest.approx(expected)
        assert len(hybrid_search(session, query, vector, model="word-hash", limit=1)) == 1
        # Among Form 4s alone, the weight would only tilt keyword against semantic: not applied.
        only = hybrid_search(session, query, vector, model="word-hash", form_type="4")
        assert accessions(only) == [form4]
        assert only[0].score == pytest.approx(2 / 61)


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
