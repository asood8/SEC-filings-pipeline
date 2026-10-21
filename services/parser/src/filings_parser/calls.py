"""Upcoming investor calls an 8-K announces: what kind, when, and where the webcast is.

Earnings releases end with a paragraph like "Oracle will hold a conference call and webcast today
to discuss these results at 4:00 p.m. Central. A live and replay webcast will be available on the
Oracle Investor Relations website at www.oracle.com/investor/." The call itself, Q&A included,
isn't in the filing (it is a webcast on the company's site), so this says only when it is and
where to listen.

The rules are narrow, and no model is involved, so what is shown is what the filing says:

- A call sentence names a call ("conference call", "earnings call", "webcast", "question and
  answer session", "Q&A", "Investor Day") and looks ahead ("will", "scheduled", "hosting"...).
  A sentence about a past event ("posted the presentation used at its Investor Day") or a replay
  ("an archived webcast will be available") doesn't count.
- Its date is a month and day ("October 21, 2026"; "Thursday, September 10" gets the year of the
  release, and a weekday that doesn't match the date rejects it), or "today", "tomorrow", or
  "that day" (the last date in the two sentences before).
- When the call sentence gives no date or time at all, the sentence before may, if it too looks
  ahead: "... at the Wells Fargo Healthcare Conference on Wednesday, September 9, 2026, beginning
  at 8:00 a.m., Eastern Time. A live audio webcast ... at www.unitedhealthgroup.com."
- A time counts only with a US time zone ("Central", "PT"...), so `starts_at` is exact or absent.
- The webcast link is the first web address in the call sentence or the four after it, else the
  one before.
- A date before the release, or more than 120 days after it, is a misreading: no call.
"""

import re
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from filings_common.parsed import CallKind, Form8K, InvestorCall

# Most specific first: "a live question and answer webcast" is a Q&A session.
_KINDS: list[tuple[CallKind, re.Pattern[str]]] = [
    ("Q&A session", re.compile(r"\bquestion[- ]and[- ]answer\b|\bQ\s?&\s?A\b", re.I)),
    ("Investor Day", re.compile(r"\binvestor day\b", re.I)),
    ("Earnings call", re.compile(r"\bearnings (?:conference )?call\b", re.I)),
    ("Conference call", re.compile(r"\bconference call\b", re.I)),
    ("Webcast", re.compile(r"\bwebcast\b", re.I)),
]
_AHEAD = re.compile(
    r"\b(?:will|scheduled|to (?:host|hold|be held)|hosting|looks forward|plans to|beginning)\b",
    re.I,
)
_REPLAY = re.compile(r"\b(?:replay|archived?|recording|recorded)\b", re.I)

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip
_MONTH_NUMBERS = {name[:3]: number for number, name in enumerate(_MONTHS, 1)}
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DATE = re.compile(
    rf"\b(?:(?P<weekday>{'|'.join(_WEEKDAYS)}),?\s+)?"
    rf"(?P<month>{'|'.join(_MONTHS)}|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{4})\b)?"
)
_RELATIVE = re.compile(
    r"\b(?:today|tonight|this (?:morning|afternoon|evening)|tomorrow|that day|the same day)\b",
    re.I,
)
# Times after "a.m."/"p.m." became "am"/"pm" (see _sentences).
_TIME = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s?(?P<half>[ap])m\b"
    r"[\s,(]*(?P<zone>Eastern|Central|Mountain|Pacific|[ECMP][SD]?T)\b",
    re.I,
)
_ZONES = {
    "E": ZoneInfo("America/New_York"),
    "C": ZoneInfo("America/Chicago"),
    "M": ZoneInfo("America/Denver"),
    "P": ZoneInfo("America/Los_Angeles"),
}
# Full addresses, www. ones, investor-site subdomains ("investor.costco.com"), and lowercase
# domains with a path ("amazon.com/ir"); not "Amazon.com" in a company name.
_URL_CHARS = r"[^\s<>\"'“”()|]"
_URL = re.compile(
    rf"\bhttps?://{_URL_CHARS}+|\bwww\.{_URL_CHARS}+"
    rf"|\b(?:investors?|ir)\.[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{{2,}}(?:/{_URL_CHARS}*)?"
    rf"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org)/{_URL_CHARS}*"
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])")

URLS_AFTER = 4
MAX_DAYS_AHEAD = 120


def find_call(form: Form8K, released_on: date) -> InvestorCall | None:
    """The first upcoming call announced in the items, then the exhibits. `released_on` is the
    day the filing went out (what "today" means)."""
    for text in [item.text for item in form.items] + [e.text for e in form.exhibits]:
        sentences = _sentences(text)
        for index in range(len(sentences)):
            if (call := _call(sentences, index, released_on)) is not None:
                return call
    return None


def _sentences(text: str) -> list[str]:
    # "a.m." and "p.m." would end sentences ("4:30 p.m. Central Time"), so they lose their dots.
    text = re.sub(r"\b([ap])\.m\.", r"\1m", text, flags=re.I)
    return [s for line in text.split("\n") for s in _SENTENCE_END.split(line.strip()) if s]


def _call(sentences: Sequence[str], index: int, released_on: date) -> InvestorCall | None:
    sentence = sentences[index]
    kind = next((kind for kind, pattern in _KINDS if pattern.search(sentence)), None)
    if kind is None or not _AHEAD.search(sentence) or _REPLAY.search(sentence):
        return None
    earlier = sentences[max(0, index - 2) : index]
    source = sentence
    if not (_DATE.search(sentence) or _RELATIVE.search(sentence) or _TIME.search(sentence)):
        # A talk announced in the sentence before, then its webcast.
        if not earlier or not _AHEAD.search(earlier[-1]):
            return None
        source = earlier[-1]
        earlier = sentences[max(0, index - 3) : index - 1]
    day = _day(source, earlier, released_on)
    if day is None or not released_on <= day <= released_on + timedelta(days=MAX_DAYS_AHEAD):
        return None
    return InvestorCall(
        kind=kind,
        day=day,
        starts_at=_starts_at(source, day),
        webcast_url=_webcast_url(sentences, index),
    )


def _day(sentence: str, earlier: Sequence[str], released_on: date) -> date | None:
    if (explicit := _DATE.search(sentence)) is not None:
        return _resolve(explicit, released_on)
    if (relative := _RELATIVE.search(sentence)) is None:
        return None
    word = relative[0].lower()
    if word in ("that day", "the same day"):
        for before in reversed(earlier):
            if dates := list(_DATE.finditer(before)):
                return _resolve(dates[-1], released_on)
        return None
    return released_on + timedelta(days=1 if word == "tomorrow" else 0)


def _resolve(match: re.Match[str], released_on: date) -> date | None:
    month = _MONTH_NUMBERS[match["month"][:3]]
    year = int(match["year"]) if match["year"] else released_on.year
    try:
        day = date(year, month, int(match["day"]))
        if not match["year"] and day < released_on - timedelta(days=7):
            day = date(year + 1, month, int(match["day"]))  # "January 28" in a December release
    except ValueError:  # "February 30"
        return None
    if match["weekday"] and _WEEKDAYS[day.weekday()] != match["weekday"]:
        return None
    return day


def _starts_at(sentence: str, day: date) -> datetime | None:
    if (clock := _TIME.search(sentence)) is None:
        return None
    hour, minute = int(clock["hour"]), int(clock["minute"] or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if clock["half"].lower() == "p" else 0)
    return datetime.combine(day, time(hour, minute), tzinfo=_ZONES[clock["zone"][0].upper()])


def _webcast_url(sentences: Sequence[str], index: int) -> str | None:
    nearby = [*sentences[index : index + 1 + URLS_AFTER], *sentences[max(0, index - 1) : index]]
    for sentence in nearby:
        if (found := _URL.search(sentence)) is not None:
            url = found[0].rstrip(".,;:!?")
            return url if url.startswith(("http://", "https://")) else f"https://{url}"
    return None
