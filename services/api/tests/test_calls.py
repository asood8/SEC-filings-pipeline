from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api import ical
from filings_api.routes import calls
from filings_common.db import (
    CallSummary,
    CallTranscript,
    Filing,
    Form8KFiling,
    session_factory,
)

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


@pytest.mark.usefixtures("more_calls")
def test_the_calendar_version_ignores_when_it_was_written(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = client.get("/calls.ics")

    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Later":
            return cls(2026, 10, 23, 12, tzinfo=UTC)

    monkeypatch.setattr(calls, "datetime", Later)
    later = client.get("/calls.ics")
    assert "DTSTAMP:20261023T120000Z" in later.text  # written later, the same calls
    assert later.headers["ETag"] == first.headers["ETag"]
    again = client.get("/calls.ics", headers={"If-None-Match": first.headers["ETag"]})
    assert (again.status_code, again.content) == (304, b"")


# --- Earnings-call Q&A summaries (ADR-0022) -------------------------------------------------------

TAKEAWAYS = {
    "headline": "Microsoft: questions on AI capacity",
    "summary": "Analysts asked about capacity and capital.",
    "key_points": ["Capacity constrained through the year"],
}


@pytest.fixture
def summarized(seeded: Engine) -> dict[str, int]:
    """Two Microsoft calls (the older summarized twice) and an Apple one."""
    with session_factory(seeded).begin() as session:
        transcripts = {
            key: CallTranscript(
                cik=cik,
                call_date=day,
                source_url=f"https://ir.example/{key}.pdf",
                text_sha256="x",
                words=9000,
                exchanges=2,
            )
            for key, cik, day in [
                ("msft-july", 789019, date(2026, 7, 29)),
                ("msft-october", 789019, date(2026, 10, 28)),
                ("aapl-october", 320193, date(2026, 10, 30)),
            ]
        }
        session.add_all(transcripts.values())
        session.flush()
        exchanges = [
            {
                "analyst": "Adam Wood",
                "firm": "Morgan Stanley",
                "question": "Capacity?",
                "answer": "Constrained, said Amy Hood.",
                "provider": "ollama",
                "model": "m",
                "ungrounded_numbers": [],
                "ungrounded_names": [],
            },
            {"analyst": "Karl Keirstead", "firm": "UBS", "failed": True},
        ]
        for key, transcript in transcripts.items():
            for created, headline in [
                (datetime(2026, 11, 1, tzinfo=UTC), "Older summary"),
                (datetime(2026, 11, 2, tzinfo=UTC), TAKEAWAYS["headline"]),
            ]:
                if key != "msft-july" and headline == "Older summary":
                    continue
                session.add(
                    CallSummary(
                        transcript_id=transcript.id,
                        provider="ollama",
                        model="llama3.2:3b" if headline != "Older summary" else "old",
                        prompt_version=1,
                        takeaways=TAKEAWAYS | {"headline": headline},
                        exchanges=exchanges,
                        ungrounded_numbers=[],
                        ungrounded_names=[],
                        created_at=created,
                    )
                )
        return {key: t.id for key, t in transcripts.items()}


def test_call_summaries_are_listed_newest_call_first_with_their_current_summary(
    client: TestClient, summarized: dict[str, int]
) -> None:
    response = client.get("/call-summaries")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [(i["ticker"], i["call_date"]) for i in items] == [
        ("AAPL", "2026-10-30"),
        ("MSFT", "2026-10-28"),
        ("MSFT", "2026-07-29"),
    ]
    july = items[2]
    assert july["headline"] == TAKEAWAYS["headline"]  # the newer of its two summaries
    assert july["model"] == "llama3.2:3b"
    assert july["source_url"] == "https://ir.example/msft-july.pdf"
    assert july["exchanges"] == [
        {
            "analyst": "Adam Wood",
            "firm": "Morgan Stanley",
            "question": "Capacity?",
            "answer": "Constrained, said Amy Hood.",
            "failed": False,
            "ungrounded_numbers": [],
        },
        {
            "analyst": "Karl Keirstead",
            "firm": "UBS",
            "question": None,
            "answer": None,
            "failed": True,
            "ungrounded_numbers": [],
        },
    ]
    assert "text" not in july  # never the transcript itself
    assert "transcript" not in july


def test_call_summaries_by_company_and_one_by_id(
    client: TestClient, summarized: dict[str, int]
) -> None:
    msft = client.get("/call-summaries", params={"ticker": "msft", "limit": 1}).json()["items"]
    assert [i["call_date"] for i in msft] == ["2026-10-28"]
    one = client.get(f"/call-summaries/{summarized['aapl-october']}")
    assert one.status_code == 200
    assert one.json()["company_name"] == "Apple Inc."
    assert client.get("/call-summaries/999999").status_code == 404
    assert client.get("/call-summaries", params={"ticker": "NOPE"}).status_code == 404
