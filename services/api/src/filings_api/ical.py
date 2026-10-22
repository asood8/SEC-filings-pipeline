"""A minimal iCalendar writer (RFC 5545) for the investor-call feed (`GET /calls.ics`).

Only what the feed needs: events with a UTC start and a duration, or a whole day. Text values
are escaped, and lines longer than 75 octets are folded, as calendar apps expect.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


@dataclass(frozen=True)
class Event:
    uid: str
    summary: str
    description: str
    day: date
    starts_at: datetime | None = None  # None: an all-day event
    duration: timedelta = timedelta(hours=1)
    url: str | None = None


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")  # a lone carriage return would end the line too
    )


def _fold(line: str) -> list[str]:
    """Lines of at most 75 octets; continuations start with a space (RFC 5545, 3.1)."""
    out: list[str] = []
    current = ""
    for char in line:
        limit = 75 if not out else 74  # a continuation's leading space counts
        if len((current + char).encode()) > limit:
            out.append(current)
            current = char
        else:
            current += char
    out.append(current)
    return [out[0], *(" " + part for part in out[1:])]


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _duration(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    return f"PT{minutes // 60}H{minutes % 60}M" if minutes % 60 else f"PT{minutes // 60}H"


def calendar(events: Iterable[Event], *, name: str, now: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SEC Filings//Investor calls//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
    ]
    for event in events:
        lines += ["BEGIN:VEVENT", f"UID:{event.uid}", f"DTSTAMP:{_utc(now)}"]
        if event.starts_at is None:
            lines += [
                f"DTSTART;VALUE=DATE:{event.day:%Y%m%d}",
                f"DTEND;VALUE=DATE:{event.day + timedelta(days=1):%Y%m%d}",
            ]
        else:
            lines += [f"DTSTART:{_utc(event.starts_at)}", f"DURATION:{_duration(event.duration)}"]
        lines += [f"SUMMARY:{_escape(event.summary)}", f"DESCRIPTION:{_escape(event.description)}"]
        if event.url:
            lines.append(f"URL:{event.url}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(f"{part}\r\n" for line in lines for part in _fold(line))
