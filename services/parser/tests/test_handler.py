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
    Company,
    Filing,
    FilingExhibit,
    FilingStatus,
    Form4Filing,
    OutboxEvent,
    session_factory,
)
from filings_common.events import FilingDiscovered, FilingParsed, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.storage import LocalObjectStore
from filings_parser.documents import RawExhibit
from filings_parser.forms import PARSERS as REAL_PARSERS
from filings_parser.forms import FormParser
from filings_parser.handler import FilingHandler

pytestmark = pytest.mark.integration

ACCESSION = "0001140361-26-000202"
RAW_KEY = f"filings/320193/{ACCESSION}/form4.xml"
FORM4_FIXTURE = Path(__file__).parents[3] / "tests/fixtures/form4/0001936006-26-000026.xml"


def fake_parse(raw: bytes, exhibits: Sequence[RawExhibit] = ()) -> dict[str, Any]:
    if raw == b"broken":
        raise ValueError("unexpected root element")
    return {"bytes": len(raw), "exhibits": [e.type for e in exhibits]}


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
        session.flush()
        session.add(
            FilingExhibit(
                accession_no=ACCESSION,
                filename="ex991.htm",
                exhibit_type="EX-99.1",
                sequence=2,
                description=None,
                object_key=f"filings/320193/{ACCESSION}/ex991.htm",
            )
        )
    objects = LocalObjectStore(tmp_path)
    objects.put(RAW_KEY, b"<ownershipDocument/>")
    objects.put(f"filings/320193/{ACCESSION}/ex991.htm", b"<p>press release</p>")
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
    assert (stored["parser_version"], stored["parsed"]) == (
        3,
        {"bytes": 20, "exhibits": ["EX-99.1"]},
    )
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


def test_forms_without_a_parser_are_acknowledged_and_stay_discovered(
    handler: FilingHandler, clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    handler(discovered(form_type="8-K"))  # no exception: not dead-lettered
    assert ParsedFilingStore(mongo_db).get(ACCESSION) is None
    with session_factory(clean_db)() as session:
        assert session.get_one(Filing, ACCESSION).status is FilingStatus.DISCOVERED
        assert session.scalars(sa.select(OutboxEvent)).all() == []


def test_real_form_4_key_fields_land_in_postgres(
    clean_db: Engine, mongo_db: Database[dict[str, Any]], tmp_path: Path
) -> None:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        session.add(Company(cik=1018724, ticker="AMZN", name="AMAZON COM INC", on_watchlist=True))
        session.flush()
        session.add(
            Filing(
                accession_no="0001936006-26-000026",
                cik=1018724,
                form_type="4",
                filed_at=date(2026, 10, 2),
                primary_doc_url="https://www.sec.gov/x/wk-form4.xml",
                raw_object_key="filings/1018724/0001936006-26-000026/wk-form4.xml",
            )
        )
    objects = LocalObjectStore(tmp_path)
    objects.put("filings/1018724/0001936006-26-000026/wk-form4.xml", FORM4_FIXTURE.read_bytes())
    real = FilingHandler(sessions, objects, ParsedFilingStore(mongo_db), REAL_PARSERS)
    event = FilingDiscovered(
        accession_no="0001936006-26-000026",
        cik=1018724,
        form_type="4",
        filed_at=date(2026, 10, 2),
        raw_object_key="filings/1018724/0001936006-26-000026/wk-form4.xml",
    )
    received = Received(Topic.FILINGS_DISCOVERED, 0, 0, event.key, event.model_dump_json().encode())
    real(received)
    real(received)  # redelivery rewrites the same key fields

    with sessions() as session:
        [row] = session.scalars(sa.select(Form4Filing)).all()
    assert (row.issuer_cik, row.issuer_ticker, row.period_of_report) == (
        1018724,
        "AMZN",
        date(2026, 10, 1),
    )
    assert row.reporting_owners == ["Herrington Douglas J"]
    assert (row.transaction_codes, row.is_10b5_1) == (["S"], True)
    stored = ParsedFilingStore(mongo_db).get("0001936006-26-000026")
    assert stored is not None
    assert stored["parsed"]["transactions"][0]["price_per_share"] == 251.5
