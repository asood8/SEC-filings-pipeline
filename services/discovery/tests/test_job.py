from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.config import EdgarSettings
from filings_common.db import Filing, OutboxEvent, session_factory
from filings_common.edgar import EdgarClient
from filings_common.events import FilingDiscovered, Topic
from filings_common.storage import LocalObjectStore
from filings_discovery.companies import parse_company_tickers_exchange, sync_companies
from filings_discovery.job import discover

# Type of the make_submissions fixture (conftest.py).
SubmissionsFactory = Callable[..., dict[str, Any]]

pytestmark = pytest.mark.integration

APPLE, MICROSOFT = 320193, 789019
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"


class NoLimit:
    def acquire(self) -> float:
        return 0.0


class FakeSec:
    """Serves canned EDGAR responses and counts requests per URL."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.hits: Counter[str] = Counter()

    def client(self) -> EdgarClient:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.hits[url] += 1
            return self.routes.get(url, httpx.Response(404))

        settings = EdgarSettings(_env_file=None, contact_email="ops@example.com", max_attempts=1)
        return EdgarClient(settings, NoLimit(), transport=httpx.MockTransport(handler))


@pytest.fixture
def fake_sec(make_submissions: SubmissionsFactory) -> FakeSec:
    apple = make_submissions(
        ("0000320193-26-000101", "8-K", "2026-09-30", "aapl-20260930.htm"),
        ("0001140361-26-000202", "4", "2026-09-29", "xslF345X05/form4.xml"),
        ("0000320193-26-000099", "10-Q", "2026-09-28", "aapl-10q.htm"),  # form not collected
        ("0000320193-26-000050", "8-K", "2026-07-01", "old-8k.htm"),  # before the lookback
    )
    return FakeSec(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(200, json=apple),
            # Microsoft's submissions request fails: it must not stop Apple's discovery.
            f"{ARCHIVE}/320193/000032019326000101/aapl-20260930.htm": httpx.Response(
                200, content=b"<html>8-K</html>", headers={"Content-Type": "text/html"}
            ),
            f"{ARCHIVE}/320193/000114036126000202/form4.xml": httpx.Response(
                200, content=b"<ownershipDocument/>", headers={"Content-Type": "text/xml"}
            ),
        }
    )


def run(engine: Engine, sec: FakeSec, store: LocalObjectStore) -> Any:
    with sec.client() as edgar:
        return discover(
            session_factory(engine), edgar, store, form_types={"4", "8-K"}, since=date(2026, 9, 1)
        )


def test_discovers_stores_and_announces_new_filings_exactly_once(
    clean_db: Engine, fake_sec: FakeSec, company_tickers: dict[str, Any], tmp_path: Path
) -> None:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        records = parse_company_tickers_exchange(company_tickers)
        sync_companies(session, records, {APPLE, MICROSOFT})
    store = LocalObjectStore(tmp_path)

    first = run(clean_db, fake_sec, store)
    second = run(clean_db, fake_sec, store)

    assert (first.companies, first.new_filings, first.failed_ciks) == (2, 2, [MICROSOFT])
    assert second.new_filings == 0
    # Documents were downloaded once; the re-run skipped known filings without fetching them.
    assert all(n == 1 for url, n in fake_sec.hits.items() if url.startswith(ARCHIVE))

    with sessions() as session:
        filings = session.scalars(sa.select(Filing).order_by(Filing.filed_at)).all()
        events = session.scalars(sa.select(OutboxEvent).order_by(OutboxEvent.id)).all()

    assert [(f.accession_no, f.form_type) for f in filings] == [
        ("0001140361-26-000202", "4"),
        ("0000320193-26-000101", "8-K"),
    ]
    form4 = filings[0]
    assert form4.primary_doc_url == f"{ARCHIVE}/320193/000114036126000202/form4.xml"
    assert store.get(form4.raw_object_key) == b"<ownershipDocument/>"

    assert [e.topic for e in events] == [Topic.FILINGS_DISCOVERED] * 2
    announced = [FilingDiscovered.model_validate(e.payload) for e in events]
    assert [a.accession_no for a in announced] == [f.accession_no for f in filings]
    assert announced[0].raw_object_key == form4.raw_object_key
    assert all(e.key == e.payload["accession_no"] for e in events)
