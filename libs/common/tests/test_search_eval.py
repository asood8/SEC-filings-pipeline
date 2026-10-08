import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.db import (
    EMBEDDING_DIMENSIONS,
    Company,
    Filing,
    FilingEmbedding,
    Summary,
    session_factory,
)
from filings_common.embeddings import EmbeddingError
from filings_common.search import rebuild_index, upsert_embedding
from filings_common.search_eval import (
    Evaluation,
    LabeledQuery,
    LabelError,
    evaluate,
    first_relevant_rank,
    form4_share,
    load_labels,
    main,
    read_only,
    recall_at,
    reciprocal_rank,
    render_per_query,
    render_summary,
    score,
)

COMMITTED_LABELS = Path(__file__).parents[3] / "config" / "search_eval.json"

CFO_8K = "0000000001-26-000001"
NOTES_8K = "0000000001-26-000002"
SALE_4 = "0000000002-26-000003"
GRANT_4 = "0000000002-26-000004"
MISSING = "0000000009-26-000009"
FORMS = {CFO_8K: "8-K", NOTES_8K: "8-K", SALE_4: "4", GRANT_4: "4"}


def labeled(query: str, *relevant: str) -> LabeledQuery:
    return LabeledQuery(query=query, relevant=relevant)


# Metrics


def test_recall_is_the_share_of_relevant_filings_in_the_top_k() -> None:
    ranking = ["a", "b", "c", "d", "e", "f"]
    assert recall_at(ranking, {"b", "f", "z"}, 5) == pytest.approx(1 / 3)
    assert recall_at(ranking, {"b", "f", "z"}, 10) == pytest.approx(2 / 3)
    assert recall_at(ranking, {"b"}, 1) == 0.0
    assert recall_at([], {"b"}, 5) == 0.0


def test_reciprocal_rank_uses_the_first_relevant_filing() -> None:
    assert first_relevant_rank(["a", "b", "c"], {"c", "b"}) == 2
    assert reciprocal_rank(["a", "b", "c"], {"c", "b"}) == 0.5
    assert first_relevant_rank(["a"], {"z"}) is None
    assert reciprocal_rank(["a"], {"z"}) == 0.0
    assert reciprocal_rank([], {"z"}) == 0.0


def test_form4_share_counts_slots_out_of_k() -> None:
    # Two of five slots are Form 4s; an empty or unknown slot is not one.
    assert form4_share([SALE_4, CFO_8K, GRANT_4], FORMS) == pytest.approx(0.4)
    assert form4_share([SALE_4, "unknown"], FORMS) == pytest.approx(0.2)
    assert form4_share([SALE_4, GRANT_4, SALE_4], FORMS, k=2) == 1.0
    assert form4_share([], FORMS) == 0.0


def test_score_averages_queries_and_measures_form4_share_only_without_relevant_form4s() -> None:
    labels = [
        labeled("CFO appointment", CFO_8K),
        labeled("insider sale", SALE_4),
        labeled("labels from another database", MISSING),
        labeled("debt", NOTES_8K, CFO_8K),
    ]
    rankings = [
        [SALE_4, CFO_8K],
        [GRANT_4, SALE_4],
        [CFO_8K],
        [NOTES_8K, *["x"] * 9, CFO_8K],  # the second one is 11th: outside the top 10
    ]
    result = score("hybrid", labels, rankings, FORMS)

    cfo, sale, missing, debt = result.queries
    assert (cfo.recall_at_5, cfo.reciprocal_rank, cfo.first_relevant_rank) == (1.0, 0.5, 2)
    assert cfo.form4_share_at_5 == pytest.approx(0.2)
    assert sale.form4_share_at_5 is None  # it looks for a Form 4
    assert missing.form4_share_at_5 is None  # unknown form: not measured
    assert (missing.recall_at_10, missing.first_relevant_rank) == (0.0, None)
    assert (debt.recall_at_5, debt.recall_at_10, debt.reciprocal_rank) == (0.5, 0.5, 1.0)
    assert debt.top == rankings[3][:10]

    summary = result.summary()
    assert summary["mode"] == "hybrid"
    assert summary["queries"] == 4
    assert summary["recall_at_5"] == pytest.approx((1 + 1 + 0 + 0.5) / 4, abs=1e-3)
    assert summary["mrr"] == pytest.approx((0.5 + 0.5 + 0 + 1) / 4, abs=1e-3)
    assert summary["form4_share_at_5"] == pytest.approx(0.1)  # (0.2 + 0) / 2
    assert summary["form4_share_queries"] == 2


def test_score_needs_one_ranking_per_query() -> None:
    with pytest.raises(ValueError, match="zip"):
        score("keyword", [labeled("q", CFO_8K)], [], FORMS)


def test_reports_render_as_markdown() -> None:
    result = score("keyword", [labeled("CFO appointment", CFO_8K)], [[SALE_4, CFO_8K]], FORMS)
    evaluation = Evaluation(
        depth=20,
        embedding_model=None,
        results=[result],
        skipped={"semantic": "no embedding model"},
        missing=[MISSING],
    )
    summary = render_summary(evaluation)
    assert summary.splitlines()[:3] == [
        "| Mode | Queries | Recall@5 | Recall@10 | MRR | Form 4 share@5 |",
        "|---|---|---|---|---|---|",
        "| keyword | 1 | 1.00 | 1.00 | 0.50 | 20% (1 queries) |",
    ]
    assert "semantic: skipped, no embedding model" in summary
    assert f"1 labeled filings are not in this database, so the labels don't fit it: {MISSING}" in (
        summary
    )
    assert (
        render_per_query(result).splitlines()[-1] == "| CFO appointment | 1 | 1.00 | 1.00 | 2 | 1 |"
    )

    report = evaluation.to_json()
    assert report["modes"][0]["per_query"][0]["first_relevant_rank"] == 2
    assert json.loads(json.dumps(report)) == report


# Label files


def write_labels(path: Path, queries: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"description": "test labels", "queries": queries}))
    return path


def test_load_labels(tmp_path: Path) -> None:
    path = write_labels(
        tmp_path / "labels.json",
        [
            {"query": "CFO appointment", "relevant": [CFO_8K], "note": "Acme's new CFO"},
            {"query": "insider sale", "relevant": [SALE_4, GRANT_4]},
        ],
    )
    labels = load_labels(path)
    assert labels == [
        LabeledQuery(query="CFO appointment", relevant=(CFO_8K,), note="Acme's new CFO"),
        LabeledQuery(query="insider sale", relevant=(SALE_4, GRANT_4)),
    ]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "Invalid JSON"),
        ('{"description": "no queries"}', "Field required"),
        ('{"queries": []}', "at least 1 item"),
        ('{"queries": [{"query": "q", "relevant": ["123"]}]}', "should match pattern"),
        ('{"queries": [{"query": "q", "relevant": []}]}', "at least 1 item"),
        (f'{{"queries": [{{"query": "  ", "relevant": ["{CFO_8K}"]}}]}}', "must not be blank"),
        (
            f'{{"queries": [{{"query": "q", "relevant": ["{CFO_8K}", "{CFO_8K}"]}}]}}',
            "lists an accession number twice",
        ),
        (
            f'{{"queries": [{{"query": "Q", "relevant": ["{CFO_8K}"]}},'
            f' {{"query": "q ", "relevant": ["{SALE_4}"]}}]}}',
            "appears twice",
        ),
        (
            f'{{"queries": [{{"query": "q", "relevant": ["{CFO_8K}"], "grade": 2}}]}}',
            "Extra inputs are not permitted",
        ),
    ],
)
def test_load_labels_rejects_bad_files(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "labels.json"
    path.write_text(content)
    with pytest.raises(LabelError, match=message):
        load_labels(path)


def test_load_labels_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(LabelError, match="can't read"):
        load_labels(tmp_path / "nope.json")


def test_the_committed_labels_load_and_cover_the_known_weak_spots() -> None:
    labels = load_labels(COMMITTED_LABELS)
    assert 10 <= len(labels) <= 20
    queries = {q.query for q in labels}
    assert {
        "CFO appointment",
        "trading plan cancelled",
        "debt offering",
        "insider purchase",
        "quarterly results",
        "leadership change",
        "share repurchase",
        "Warren Buffett",
    } <= queries
    assert all(q.note for q in labels), "every label says why its filings are relevant"


def test_main_rejects_a_bad_label_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "labels.json"
    path.write_text('{"queries": []}')
    assert main(["--labels", str(path)]) == 2
    assert "not a valid label file" in capsys.readouterr().err


# Against a database


class StubEmbedder:
    """Fixed query vectors, so the semantic ranking is known."""

    model = "stub"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("an evaluation never embeds documents")

    def embed_query(self, text: str) -> list[float]:
        if self.fail:
            raise EmbeddingError("embedding server down")
        return QUERY_VECTORS[text]


def vector(*weights: float) -> list[float]:
    return [*weights, *[0.0] * (EMBEDDING_DIMENSIONS - len(weights))]


# Each filing's embedding is one axis: (CFO_8K, NOTES_8K, SALE_4).
QUERY_VECTORS = {
    "CFO appointment": vector(1.0, 0.1, 0.5),  # semantic order: CFO_8K, SALE_4, NOTES_8K
    "note offering": vector(0.1, 1.0, 0.0),
}
CORPUS = [
    (CFO_8K, 1, "8-K", "Acme elects new chief financial officer", "Acme elected Jane Doe."),
    (NOTES_8K, 1, "8-K", "Acme closes note offering", "Acme sold $1 billion of notes."),
    (SALE_4, 2, "4", "Bob Smith sold 1,000 GLOB shares", "Bob Smith (CFO) sold shares."),
]
LABELS = [labeled("CFO appointment", CFO_8K), labeled("note offering", NOTES_8K, MISSING)]


@pytest.fixture
def corpus(clean_db: Engine) -> Engine:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        session.add_all(
            [
                Company(cik=1, ticker="ACME", name="Acme Corp", on_watchlist=True),
                Company(cik=2, ticker="GLOB", name="Globex Inc", on_watchlist=True),
            ]
        )
        session.flush()
        for accession_no, cik, form, headline, text in CORPUS:
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=cik,
                    form_type=form,
                    filed_at=date(2026, 9, 1),
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
                    summary={"headline": headline, "summary": text, "key_points": []},
                )
            )
    rebuild_index(sessions, None)
    with sessions.begin() as session:
        for axis, (accession_no, *_) in enumerate(CORPUS):
            upsert_embedding(session, accession_no, "stub", vector(*[0.0] * axis, 1.0))
    return clean_db


@pytest.mark.integration
def test_evaluate_runs_every_mode_like_the_api(corpus: Engine) -> None:
    with read_only(corpus)() as session:
        evaluation = evaluate(session, LABELS, embedder=StubEmbedder())

    assert [r.mode for r in evaluation.results] == ["keyword", "semantic", "hybrid"]
    assert (evaluation.skipped, evaluation.missing) == ({}, [MISSING])
    keyword, semantic, hybrid = ({q.query: q for q in r.queries} for r in evaluation.results)
    # No summary says "appointment"; the any-word fallback finds the Form 4 that says "CFO" and,
    # through its keywords, the 8-K that says "chief financial officer" (equal ranks: by accession).
    assert keyword["CFO appointment"].top == [CFO_8K, SALE_4]
    assert keyword["CFO appointment"].form4_share_at_5 == pytest.approx(0.2)
    assert semantic["CFO appointment"].top == [CFO_8K, SALE_4, NOTES_8K]
    assert hybrid["CFO appointment"].top == [CFO_8K, SALE_4, NOTES_8K]
    assert hybrid["CFO appointment"].first_relevant_rank == 1
    assert semantic["note offering"].recall_at_5 == 0.5  # one of its two filings doesn't exist


@pytest.mark.integration
@pytest.mark.parametrize(
    ("embedder", "reason"),
    [(None, "EMBEDDING_MODEL is unset"), (StubEmbedder(fail=True), "embedding server down")],
)
def test_evaluate_skips_semantic_modes_without_a_working_embedder(
    corpus: Engine, embedder: StubEmbedder | None, reason: str
) -> None:
    with read_only(corpus)() as session:
        evaluation = evaluate(session, LABELS, embedder=embedder)
    assert [r.mode for r in evaluation.results] == ["keyword"]
    assert set(evaluation.skipped) == {"semantic", "hybrid"}
    assert all(reason in why for why in evaluation.skipped.values())


@pytest.mark.integration
def test_evaluation_sessions_cannot_write(corpus: Engine) -> None:
    with read_only(corpus)() as session, pytest.raises(sa.exc.DBAPIError, match="read-only"):
        session.execute(sa.delete(FilingEmbedding))
    with session_factory(corpus)() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FilingEmbedding)) == 3


@pytest.mark.integration
def test_main_prints_markdown_and_writes_json(
    corpus: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", corpus.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EMBEDDING_MODEL", "")  # whatever a developer's .env says
    labels = write_labels(
        tmp_path / "labels.json",
        [{"query": q.query, "relevant": list(q.relevant)} for q in LABELS],
    )
    out = tmp_path / "results" / "eval.json"

    assert main(["--labels", str(labels), "--per-query", "--json", str(out)]) == 0

    printed = capsys.readouterr().out
    # Both queries find a filing first (MRR 1); "note offering" can't find its missing one.
    assert "| keyword | 2 | 0.75 | 0.75 | 1.00 | 20% (1 queries) |" in printed
    assert "semantic: skipped" in printed
    assert "#### keyword" in printed
    report = json.loads(out.read_text())
    assert report["labels"] == str(labels)
    assert [m["mode"] for m in report["modes"]] == ["keyword"]
    assert report["missing"] == [MISSING]
