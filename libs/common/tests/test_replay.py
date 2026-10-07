from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from pymongo.database import Database
from sqlalchemy.engine import Engine

from filings_common.db import Company, Filing, FilingStatus, OutboxEvent, session_factory
from filings_common.events import FilingDiscovered, FilingParsed
from filings_common.mongo import ParsedFilingStore
from filings_common.replay import replay_discovered, replay_parsed

pytestmark = pytest.mark.integration

FILINGS = [
    ("0000320193-26-000001", "8-K", FilingStatus.DISCOVERED),
    ("0000320193-26-000002", "8-K", FilingStatus.PARSED),
    ("0000320193-26-000003", "4", FilingStatus.READY),
    ("0000320193-26-000004", "4", FilingStatus.DISCOVERED),
]


@pytest.fixture
def seeded(clean_db: Engine) -> Engine:
    with session_factory(clean_db).begin() as session:
        session.add(Company(cik=320193, ticker="AAPL", name="Apple Inc.", on_watchlist=True))
        session.flush()
        for n, (accession_no, form, status) in enumerate(FILINGS, start=1):
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=320193,
                    form_type=form,
                    filed_at=date(2026, 9, n),
                    primary_doc_url=f"https://www.sec.gov/{accession_no}",
                    raw_object_key=f"filings/320193/{accession_no}/doc",
                    status=status,
                )
            )
    return clean_db


def outbox(engine: Engine) -> list[OutboxEvent]:
    with session_factory(engine)() as session:
        return list(session.scalars(sa.select(OutboxEvent).order_by(OutboxEvent.id)))


def test_replay_discovered_filters_by_form_and_status(seeded: Engine) -> None:
    sessions = session_factory(seeded)
    count = replay_discovered(sessions, form_type="8-K", statuses=[FilingStatus.DISCOVERED])
    assert count == 1
    [row] = outbox(seeded)
    event = FilingDiscovered.model_validate(row.payload)
    assert (row.topic, event.accession_no) == ("filings.discovered", "0000320193-26-000001")
    assert event.raw_object_key == "filings/320193/0000320193-26-000001/doc"


def test_replay_one_filing_by_accession_number(seeded: Engine) -> None:
    sessions = session_factory(seeded)
    assert replay_discovered(sessions, accession_nos=["0000320193-26-000001", "missing"]) == 1
    assert [r.key for r in outbox(seeded)] == ["0000320193-26-000001"]


def test_replay_discovered_everything_in_filing_order(seeded: Engine) -> None:
    assert replay_discovered(session_factory(seeded)) == 4
    assert [r.key for r in outbox(seeded)] == [f[0] for f in FILINGS]


def test_replay_parsed_uses_stored_parser_versions_and_skips_unparsed(
    seeded: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    store = ParsedFilingStore(mongo_db)
    # 000002 (parsed) has a payload; 000003 (ready) has none, so it is skipped with a warning.
    store.upsert(
        accession_no="0000320193-26-000002",
        cik=320193,
        form_type="8-K",
        parser_version=7,
        parsed={},
    )
    assert replay_parsed(session_factory(seeded), store) == 1
    [row] = outbox(seeded)
    event = FilingParsed.model_validate(row.payload)
    assert (row.topic, event.accession_no, event.parser_version) == (
        "filings.parsed",
        "0000320193-26-000002",
        7,
    )
