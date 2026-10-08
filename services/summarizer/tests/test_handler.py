from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pymongo.database import Database
from sqlalchemy.engine import Engine

from filings_common.consumer import PermanentError, Received
from filings_common.db import (
    EMBEDDING_DIMENSIONS,
    Company,
    Filing,
    FilingEmbedding,
    FilingSearch,
    FilingStatus,
    OutboxEvent,
    Summary,
    session_factory,
)
from filings_common.embeddings import EmbeddingError
from filings_common.events import FilingParsed, FilingReady, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.search import SearchDocument, rebuild_index, text_sha256
from filings_common.summaries import FilingSummary
from filings_parser.form4 import parse
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.handler import SummaryHandler
from filings_summarizer.summarizers import Form4TemplateSummarizer, SummaryResult

SUMMARIZERS = {"4": Form4TemplateSummarizer()}

pytestmark = pytest.mark.integration

ACCESSION = "0001936006-26-000026"
FIXTURE = Path(__file__).parents[3] / "tests" / "fixtures" / "form4" / f"{ACCESSION}.xml"


class FixedSummarizer:
    """Stands in for a new prompt version of some summarizer."""

    def __init__(self, prompt_version: int) -> None:
        self.prompt_version = prompt_version

    def summarize(self, payload: dict[str, Any], context: FilingContext) -> SummaryResult:
        summary = FilingSummary(headline="v2", summary="Second take of $4.25 billion.")
        return SummaryResult(
            summary, "template", "form4-template", self.prompt_version, ["4.25 billion"]
        )


@pytest.fixture
def setup(clean_db: Engine, mongo_db: Database[dict[str, Any]]) -> tuple[Engine, ParsedFilingStore]:
    with session_factory(clean_db).begin() as session:
        session.add(Company(cik=1018724, ticker="AMZN", name="AMAZON COM INC", on_watchlist=True))
        session.flush()
        session.add(
            Filing(
                accession_no=ACCESSION,
                cik=1018724,
                form_type="4",
                filed_at=date(2026, 10, 2),
                primary_doc_url="https://www.sec.gov/x.xml",
                raw_object_key="filings/x.xml",
                status=FilingStatus.PARSED,
            )
        )
    store = ParsedFilingStore(mongo_db)
    store.upsert(
        accession_no=ACCESSION,
        cik=1018724,
        form_type="4",
        parser_version=1,
        parsed=parse(FIXTURE.read_bytes()),
    )
    return clean_db, store


def parsed_event(form_type: str = "4", accession_no: str = ACCESSION) -> Received:
    event = FilingParsed(
        accession_no=accession_no, cik=1018724, form_type=form_type, parser_version=1
    )
    return Received(Topic.FILINGS_PARSED, 0, 0, accession_no, event.model_dump_json().encode())


def rows(engine: Engine) -> tuple[list[Summary], list[OutboxEvent], FilingStatus]:
    with session_factory(engine)() as session:
        summaries = list(session.scalars(sa.select(Summary).order_by(Summary.id)))
        events = list(session.scalars(sa.select(OutboxEvent).order_by(OutboxEvent.id)))
        return summaries, events, session.get_one(Filing, ACCESSION).status


def test_summarizes_marks_ready_and_announces_once(
    setup: tuple[Engine, ParsedFilingStore], measured: Any
) -> None:
    engine, store = setup
    handler = SummaryHandler(session_factory(engine), store, SUMMARIZERS)
    handler(parsed_event())
    handler(parsed_event())  # redelivery
    assert measured.total("filings.ready", {"filings.form_type": "4"}) == 1  # counted once

    summaries, events, status = rows(engine)
    assert status is FilingStatus.READY
    [summary] = summaries
    assert (summary.provider, summary.model, summary.prompt_version) == (
        "template",
        "form4-template",
        1,
    )
    assert FilingSummary.model_validate(summary.summary).headline.startswith(
        "Douglas J Herrington sold"
    )
    [event] = events
    assert event.topic == Topic.FILINGS_READY
    assert FilingReady.model_validate(event.payload).summary_id == summary.id


def test_new_prompt_version_adds_a_summary_without_announcing_again(
    setup: tuple[Engine, ParsedFilingStore],
) -> None:
    engine, store = setup
    SummaryHandler(session_factory(engine), store, SUMMARIZERS)(parsed_event())
    SummaryHandler(session_factory(engine), store, {"4": FixedSummarizer(2)})(parsed_event())

    summaries, events, _ = rows(engine)
    assert [s.prompt_version for s in summaries] == [1, 2]
    assert [s.ungrounded_numbers for s in summaries] == [None, ["4.25 billion"]]
    assert len(events) == 1


def test_forms_without_a_summarizer_stay_parsed(setup: tuple[Engine, ParsedFilingStore]) -> None:
    engine, store = setup
    SummaryHandler(session_factory(engine), store, {})(parsed_event())
    summaries, events, status = rows(engine)
    assert (summaries, events, status) == ([], [], FilingStatus.PARSED)


@pytest.mark.parametrize(
    ("received", "reason"),
    [
        (Received(Topic.FILINGS_PARSED, 0, 0, ACCESSION, b"{}"), "invalid filings.parsed event"),
        (parsed_event(accession_no="0000000000-26-000001"), "no parsed payload"),
    ],
)
def test_unprocessable_events_are_permanent_errors(
    setup: tuple[Engine, ParsedFilingStore], received: Received, reason: str
) -> None:
    engine, store = setup
    with pytest.raises(PermanentError, match=reason):
        SummaryHandler(session_factory(engine), store, SUMMARIZERS)(received)


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingError("embedding server down")
        return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def search_rows(engine: Engine) -> tuple[list[FilingSearch], list[FilingEmbedding]]:
    with session_factory(engine)() as session:
        return (
            list(session.scalars(sa.select(FilingSearch))),
            list(session.scalars(sa.select(FilingEmbedding))),
        )


def test_new_summaries_are_indexed_for_search(setup: tuple[Engine, ParsedFilingStore]) -> None:
    engine, store = setup
    SummaryHandler(session_factory(engine), store, SUMMARIZERS, FakeEmbedder())(parsed_event())
    [document], [embedding] = search_rows(engine)
    assert document.title.startswith("Douglas J Herrington sold")
    assert (document.company, document.ticker) == ("Amazon.com Inc", "AMZN")
    assert document.body.endswith("Form 4 insider trading report")
    assert "sell sale" in document.keywords.splitlines()  # the template says "sold"
    assert (embedding.accession_no, embedding.model) == (ACCESSION, "fake-embed")
    text = SearchDocument(document.title, document.company, document.body, document.ticker).text()
    assert embedding.text_sha256 == text_sha256(text)


def test_the_summarizer_and_filings_index_write_the_same_document(
    setup: tuple[Engine, ParsedFilingStore],
) -> None:
    engine, store = setup
    sessions = session_factory(engine)
    SummaryHandler(sessions, store, SUMMARIZERS, FakeEmbedder())(parsed_event())
    [written], _ = search_rows(engine)

    # The company's name is "AMAZON COM INC" in the companies table, which filings-index reads.
    assert rebuild_index(sessions, FakeEmbedder()) == (1, 0)  # same text: nothing re-embedded
    [rebuilt], _ = search_rows(engine)
    fields = ("title", "company", "ticker", "body", "keywords")
    assert [getattr(rebuilt, f) for f in fields] == [getattr(written, f) for f in fields]


def test_embedding_failure_does_not_block_the_summary(
    setup: tuple[Engine, ParsedFilingStore],
) -> None:
    engine, store = setup
    handler = SummaryHandler(session_factory(engine), store, SUMMARIZERS, FakeEmbedder(fail=True))
    handler(parsed_event())
    documents, embeddings = search_rows(engine)
    assert len(documents) == 1  # still keyword-searchable
    assert embeddings == []
    _, events, status = rows(engine)
    assert status is FilingStatus.READY
    assert len(events) == 1
