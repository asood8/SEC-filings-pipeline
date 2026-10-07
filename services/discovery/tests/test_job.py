from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from sqlalchemy.engine import Engine

from filings_common.config import EdgarSettings
from filings_common.db import Filing, FilingExhibit, OutboxEvent, session_factory
from filings_common.edgar import EdgarClient
from filings_common.events import FilingDiscovered, Topic
from filings_common.storage import LocalObjectStore
from filings_discovery.companies import parse_company_tickers_exchange, sync_companies
from filings_discovery.job import backfill_exhibits, discover

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

    def __init__(self, routes: dict[str, httpx2.Response]) -> None:
        self.routes = routes
        self.hits: Counter[str] = Counter()

    def client(self) -> EdgarClient:
        def handler(request: httpx2.Request) -> httpx2.Response:
            url = str(request.url)
            self.hits[url] += 1
            return self.routes.get(url, httpx2.Response(404))

        settings = EdgarSettings(_env_file=None, contact_email="ops@example.com", max_attempts=1)
        return EdgarClient(settings, NoLimit(), transport=httpx2.MockTransport(handler))


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
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx2.Response(200, json=apple),
            # Microsoft's submissions request fails: it must not stop Apple's discovery.
            f"{ARCHIVE}/320193/000032019326000101/aapl-20260930.htm": httpx2.Response(
                200, content=b"<html>8-K</html>", headers={"Content-Type": "text/html"}
            ),
            f"{ARCHIVE}/320193/000114036126000202/form4.xml": httpx2.Response(
                200, content=b"<ownershipDocument/>", headers={"Content-Type": "text/xml"}
            ),
            # The 8-K's document list (same layout as EDGAR's index-headers pages) and its
            # press release. The contract (EX-10.1) must not be downloaded.
            f"{ARCHIVE}/320193/000032019326000101/0000320193-26-000101-index-headers.html": (
                httpx2.Response(200, text=index_headers_page())
            ),
            f"{ARCHIVE}/320193/000032019326000101/pressrelease.htm": httpx2.Response(
                200, content=b"<p>Results</p>", headers={"Content-Type": "text/html"}
            ),
        }
    )


def index_headers_page() -> str:
    documents = [
        ("8-K", 1, "aapl-20260930.htm", "8-K"),
        ("EX-99.1", 2, "pressrelease.htm", "PRESS RELEASE"),
        ("EX-10.1", 3, "agreement.htm", "CREDIT AGREEMENT"),
    ]
    blocks = "".join(
        f"&lt;DOCUMENT&gt;\n&lt;TYPE&gt;{t}\n&lt;SEQUENCE&gt;{n}\n&lt;FILENAME&gt;{f}\n"
        f'&lt;DESCRIPTION&gt;{d}\n&lt;TEXT&gt;\n<a href="{f}">{f}</a><br>\n&lt;/DOCUMENT&gt;\n'
        for t, n, f, d in documents
    )
    return f"<HTML><BODY><!--\n<SEC-HEADER>hdr\n--><PRE>{blocks}</PRE></BODY></HTML>"


def run(
    engine: Engine,
    sec: FakeSec,
    store: LocalObjectStore,
    exhibit_form_types: set[str] | None = None,
) -> Any:
    with sec.client() as edgar:
        return discover(
            session_factory(engine),
            edgar,
            store,
            form_types={"4", "8-K"},
            since=date(2026, 9, 1),
            exhibit_form_types={"8-K"} if exhibit_form_types is None else exhibit_form_types,
        )


def seed_companies(engine: Engine, company_tickers: dict[str, Any]) -> None:
    with session_factory(engine).begin() as session:
        records = parse_company_tickers_exchange(company_tickers)
        sync_companies(session, records, {APPLE, MICROSOFT})


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

    with sessions() as session:
        [exhibit] = session.scalars(sa.select(FilingExhibit)).all()
    assert (exhibit.accession_no, exhibit.exhibit_type, exhibit.filename) == (
        "0000320193-26-000101",
        "EX-99.1",
        "pressrelease.htm",
    )
    assert store.get(exhibit.object_key) == b"<p>Results</p>"
    assert not any("agreement.htm" in url for url in fake_sec.hits)
    assert not any("000114036126000202-index" in url for url in fake_sec.hits)  # Form 4: none


def test_new_filings_are_counted_by_form_type_and_a_run_without_any_reports_zero(
    clean_db: Engine,
    fake_sec: FakeSec,
    company_tickers: dict[str, Any],
    tmp_path: Path,
    measured: Any,
) -> None:
    seed_companies(clean_db, company_tickers)
    store = LocalObjectStore(tmp_path)
    run(clean_db, fake_sec, store)
    assert measured.total("filings.discovered", {"filings.form_type": "4"}) == 1
    assert measured.total("filings.discovered", {"filings.form_type": "8-K"}) == 1

    run(clean_db, fake_sec, store)  # nothing new: zeros, for the "no filings" alert to see
    assert measured.total("filings.discovered") == 2
    assert measured.attribute_sets("filings.discovered") == [
        {"filings.form_type": "4"},
        {"filings.form_type": "8-K"},
    ]


def test_backfill_fetches_exhibits_for_filings_that_have_none(
    clean_db: Engine, fake_sec: FakeSec, company_tickers: dict[str, Any], tmp_path: Path
) -> None:
    seed_companies(clean_db, company_tickers)
    store = LocalObjectStore(tmp_path)
    run(clean_db, fake_sec, store, exhibit_form_types=set())  # discovered before exhibits existed
    sessions = session_factory(clean_db)

    with fake_sec.client() as edgar:
        assert backfill_exhibits(sessions, edgar, store, form_types={"8-K"}) == (1, 1)
        assert backfill_exhibits(sessions, edgar, store, form_types={"8-K"}) == (0, 0)
    with sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FilingExhibit)) == 1


def test_each_new_filing_gets_its_own_trace_linked_to_the_poll(
    clean_db: Engine,
    fake_sec: FakeSec,
    company_tickers: dict[str, Any],
    tmp_path: Path,
    spans: InMemorySpanExporter,
) -> None:
    seed_companies(clean_db, company_tickers)
    run(clean_db, fake_sec, LocalObjectStore(tmp_path))

    finished = spans.get_finished_spans()
    [poll] = [s for s in finished if s.name == "discovery poll"]
    apple, microsoft = (s for s in finished if s.name == "poll company")
    filings = {
        (s.attributes or {})["filings.accession_no"]: s
        for s in finished
        if s.name == "discover filing"
    }
    assert set(filings) == {"0001140361-26-000202", "0000320193-26-000101"}

    # The poll is one trace: a span per company, the failed one marked failed.
    assert poll.parent is None
    assert {apple.context.trace_id, microsoft.context.trace_id} == {poll.context.trace_id}
    assert (poll.attributes or {})["filings.new_filings"] == 2
    assert microsoft.status.status_code is StatusCode.ERROR

    # Each filing is the root of a trace of its own, linked to the company poll that found it,
    # with its SEC requests and stores inside it.
    for filing in filings.values():
        assert filing.parent is None
        assert filing.context.trace_id != poll.context.trace_id
        assert [link.context for link in filing.links] == [apple.context]
        inside = [s.name for s in finished if s.context.trace_id == filing.context.trace_id]
        assert {"GET", "object_store put", "record filing"} <= set(inside)
    assert len({f.context.trace_id for f in filings.values()}) == 2

    # Its outbox event carries that trace, from the span that recorded it.
    with session_factory(clean_db)() as session:
        events = session.scalars(sa.select(OutboxEvent)).all()
    for event in events:
        assert event.trace_context is not None
        _, trace_id, span_id, _ = event.trace_context["traceparent"].split("-")
        filing = filings[event.key]
        assert trace_id == trace.format_trace_id(filing.context.trace_id)
        [record] = [
            s
            for s in finished
            if s.name == "record filing" and trace.format_span_id(s.context.span_id) == span_id
        ]
        assert record.parent is not None
        assert record.parent.span_id == filing.context.span_id
