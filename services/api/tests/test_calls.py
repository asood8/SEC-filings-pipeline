from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api import ical
from filings_api.routes import calls
from filings_common.db import Filing, Form8KFiling, session_factory

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 22)
MSFT_EARLY = "0000789019-26-000001"  # announced the 10-28 call weeks ahead, without a time
APPLE_LATER = "0000320193-26-000050"  # a January earnings call, beyond 60 days
APPLE_PAST = "0000320193-26-000040"  # an investor day that already happened


@pytest.fixture
def more_calls(seeded: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calls, "_today", lambda: TODAY)
    with session_factory(seeded).begin() as session:
        for accession_no, cik, filed in [
            (MSFT_EARLY, 789019, date(2026, 10, 1)),
            (APPLE_LATER, 320193, date(2026, 10, 20)),
            (APPLE_PAST, 320193, date(2026, 9, 25)),
        ]:
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=cik,
                    form_type="8-K",
                    filed_at=filed,
                    primary_doc_url=f"https://www.sec.gov/Archives/{accession_no}.htm",
                    raw_object_key=f"filings/{cik}/{accession_no}/doc",
                )
            )
        session.flush()
        session.add_all(
            [
                Form8KFiling(
                    accession_no=MSFT_EARLY,
                    items=["7.01"],
                    investor_call={"kind": "Earnings call", "day": "2026-10-28"},
                ),
                Form8KFiling(
                    accession_no=APPLE_LATER,
                    items=["8.01"],
                    investor_call={
                        "kind": "Earnings call",
                        "day": "2027-01-28",
                        "starts_at": "2027-01-28T17:00:00-05:00",
                        "webcast_url": "https://investor.apple.com",
                    },
                ),
                Form8KFiling(
                    accession_no=APPLE_PAST,
                    items=["7.01"],
                    investor_call={"kind": "Investor Day", "day": "2026-10-01"},
                ),
            ]
        )


@pytest.mark.usefixtures("more_calls")
def test_upcoming_calls_one_per_company_and_day(client: TestClient, ids: object) -> None:
    body = client.get("/calls").json()
    assert (body["since"], body["until"]) == ("2026-10-22", "2026-12-21")
    # The Microsoft call was announced twice; the announcement with a time wins. The January
    # Apple call is beyond 60 days, and the past investor day is gone.
    assert body["items"] == [
        {
            "kind": "Earnings call",
            "day": "2026-10-28",
            "starts_at": "2026-10-28T14:30:00-07:00",
            "webcast_url": "https://www.microsoft.com/investor",
            "ticker": "MSFT",
            "company_name": "Microsoft Corp",
            "accession_no": "0000789019-26-000003",
        }
    ]


@pytest.mark.usefixtures("more_calls")
def test_days_reaches_further_ahead(client: TestClient) -> None:
    items = client.get("/calls", params={"days": 120}).json()["items"]
    assert [(c["ticker"], c["day"]) for c in items] == [
        ("MSFT", "2026-10-28"),
        ("AAPL", "2027-01-28"),
    ]
    assert client.get("/calls", params={"days": 121}).status_code == 422


@pytest.mark.usefixtures("more_calls")
def test_the_calendar_feed(client: TestClient) -> None:
    response = client.get("/calls.ics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    text = response.text
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    unfolded = text.replace("\r\n ", "")
    # The past investor day stays (30 days back), as an all-day event.
    assert unfolded.count("BEGIN:VEVENT") == 3
    assert "UID:0000789019-26-000003-20261028@sec-filings" in unfolded
    assert "DTSTART:20261028T213000Z\r\nDURATION:PT1H" in unfolded  # 2:30 p.m. Pacific
    assert "DTSTART;VALUE=DATE:20261001\r\nDTEND;VALUE=DATE:20261002" in unfolded
    assert "SUMMARY:MSFT earnings call" in unfolded
    assert "Webcast: https://www.microsoft.com/investor\\n" in unfolded
    assert "Filing: https://www.sec.gov/Archives/0000789019-26-000003.htm" in unfolded


def test_text_is_escaped_and_long_lines_folded() -> None:
    event = ical.Event(
        uid="x@sec-filings",
        summary="A, B; C\\D",
        description="line one\nline two\r" + "é" * 60,
        day=date(2026, 10, 28),
    )
    text = ical.calendar([event], name="Test", now=datetime(2026, 10, 22, tzinfo=UTC))
    assert "SUMMARY:A\\, B\\; C\\\\D\r\n" in text
    lines = text.split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines)
    description = "".join(
        line[1:] if line.startswith(" ") else line
        for line in lines
        if line.startswith(("DESCRIPTION", " "))
    )
    assert description == "DESCRIPTION:line one\\nline two\\n" + "é" * 60


@pytest.mark.usefixtures("more_calls")
def test_one_companys_calls(client: TestClient) -> None:
    items = client.get("/calls", params={"days": 120, "ticker": "aapl"}).json()["items"]
    assert [(c["ticker"], c["day"]) for c in items] == [("AAPL", "2027-01-28")]
    assert client.get("/calls", params={"ticker": "NOPE"}).status_code == 404

    text = client.get("/calls.ics", params={"ticker": "msft"}).text.replace("\r\n ", "")
    assert "X-WR-CALNAME:SEC filings: MSFT investor calls\r\n" in text
    assert text.count("BEGIN:VEVENT") == 1  # Microsoft's call only, not Apple's two
    assert client.get("/calls.ics", params={"ticker": "NOPE"}).status_code == 404
