from datetime import date
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pymongo.database import Database
from sqlalchemy.engine import Engine

from filings_common.consumer import PermanentError, Received
from filings_common.db import Company, Filing, FilingStatus, OutboxEvent, session_factory
from filings_common.events import FilingDiscovered, FilingParsed, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.storage import LocalObjectStore
from filings_parser.forms import FormParser
from filings_parser.handler import FilingHandler

pytestmark = pytest.mark.integration

ACCESSION = "0001140361-26-000202"
RAW_KEY = f"filings/320193/{ACCESSION}/form4.xml"


def fake_parse(raw: bytes) -> dict[str, Any]:
    if raw == b"broken":
        raise ValueError("unexpected root element")
    return {"bytes": len(raw)}


PARSERS = {"4": FormParser(version=3, parse=fake_parse)}


@pytest.fixture
def handler(clean_db: Engine, mongo_db: Database[dict[str, Any]], tmp_path: Path) -> FilingHandler:
    with session_factory(clean_db).begin() as session:
        session.add(Company(cik=320193, ticker="AAPL", name="Apple Inc.", on_watchlist=True))
        session.flush()
        session.add(
            Filing(
                accession_no=ACCESSION,
                cik=320193,
                form_type="4",
                filed_at=date(2026, 9, 29),
                primary_doc_url="https://www.sec.gov/x/form4.xml",
                raw_object_key=RAW_KEY,
            )
        )
    objects = LocalObjectStore(tmp_path)
    objects.put(RAW_KEY, b"<ownershipDocument/>")
    return FilingHandler(session_factory(clean_db), objects, ParsedFilingStore(mongo_db), PARSERS)


def discovered(**overrides: object) -> Received:
    fields: dict[str, object] = {
        "accession_no": ACCESSION,
        "cik": 320193,
        "form_type": "4",
        "filed_at": date(2026, 9, 29),
        "raw_object_key": RAW_KEY,
    }
    event = FilingDiscovered.model_validate(fields | overrides)
    return Received(
        topic=Topic.FILINGS_DISCOVERED,
        partition=0,
        offset=0,
        key=ACCESSION,
        value=event.model_dump_json().encode(),
    )


def test_parses_stores_and_announces_once_even_if_redelivered(
    handler: FilingHandler, clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    handler(discovered())
    handler(discovered())  # redelivery

    stored = ParsedFilingStore(mongo_db).get(ACCESSION)
    assert stored is not None
    assert (stored["parser_version"], stored["parsed"]) == (3, {"bytes": 20})
    with session_factory(clean_db)() as session:
        assert session.get_one(Filing, ACCESSION).status is FilingStatus.PARSED
        events = session.scalars(sa.select(OutboxEvent)).all()
    assert [e.topic for e in events] == [Topic.FILINGS_PARSED]
    assert FilingParsed.model_validate(events[0].payload).parser_version == 3


@pytest.mark.parametrize(
    ("received", "reason"),
    [
        (
            Received(Topic.FILINGS_DISCOVERED, 0, 0, ACCESSION, b"not json"),
            "invalid filings.discovered event",
        ),
        (discovered(form_type="8-K"), "no parser for form type '8-K'"),
        (discovered(raw_object_key="filings/missing.xml"), "raw document missing"),
        (discovered(accession_no="0000000000-26-000001"), "no filing row"),
    ],
)
def test_unprocessable_events_are_permanent_errors(
    handler: FilingHandler, received: Received, reason: str
) -> None:
    with pytest.raises(PermanentError, match=reason):
        handler(received)


def test_parser_failures_are_permanent_and_leave_the_filing_unparsed(
    handler: FilingHandler, clean_db: Engine, tmp_path: Path
) -> None:
    LocalObjectStore(tmp_path).put(RAW_KEY, b"broken")
    with pytest.raises(PermanentError, match="unexpected root element"):
        handler(discovered())
    with session_factory(clean_db)() as session:
        assert session.get_one(Filing, ACCESSION).status is FilingStatus.DISCOVERED
