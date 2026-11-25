"""Earnings-call transcripts that companies publish themselves (ADR-0022): cleaning the extracted
text, splitting it into the prepared remarks and the Q&A exchanges, and finding the call's date.

Every transcript labels the operator's turns ("Operator:", "OPERATOR:", "[OPERATOR]"). An exchange
starts at an operator turn that names the next analyst and runs to the next such turn:

- "Our next question comes from the line of Erika Najarian with UBS."
- "We'll move next to Manan Gosalia with Morgan Stanley."
- "Asad Haider, Goldman Sachs." (J&J's operator only gives the name and firm)

Everything before the first one is the prepared remarks. No speaker-identification model: the
operator's introductions say who asks, and the transcript's own labels say who answers.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

# Bump when splitting changes, so stored transcripts can be counted again.
# 2: firms written with initials ("J.P. Morgan"); 3: a firm starting with one initial ("B. Riley"),
# names with "de", "van"... ("Carlos de Alba"), and a first question after long queue instructions
SPLITTER_VERSION = 3

# Curly apostrophes and no-break spaces, as plain ones.
_PLAIN = str.maketrans({chr(0x2018): "'", chr(0x2019): "'", chr(0xA0): " ", chr(0x202F): " "})
_DOT_LEADERS = re.compile(r"\.{4,}[ .]*")  # FactSet's "................" between turns
_BOILERPLATE = re.compile(
    r"^.*(?:1-877-FACTSET|www\.callstreet\.com|FactSet CallStreet, LLC).*$\n?"
    r"|^\s*\d{1,3}\s*$\n?",  # a page number on its own line
    re.MULTILINE,
)
_OPERATOR = re.compile(r"\[?\b(?:OPERATOR|Operator)\b(?!\s+Instructions)\s*[:\]]?")
_WORD = r"[A-Z][\w.'-]*"
_PARTICLE = r"(?:de|da|del|della|di|do|dos|du|van|von|der|den|ten|ter|le|la)"  # "Carlos de Alba"
_NAME = rf"{_WORD}(?:\s+(?:{_PARTICLE}\s+)?{_WORD}){{1,3}}"
# No dots, so a firm's name ends at its sentence's period; but initials ("J.P." in J.P. Morgan)
# are one word, and so is one initial before another word of the name ("B." in B. Riley).
_FIRM_WORD = r"(?:(?:[A-Z]\.){2,}|[A-Z]\.(?=\s+[A-Z][a-z])|[A-Z][\w&'-]*)"
_FIRM = rf"{_FIRM_WORD}(?:\s+(?:of\s+|and\s+|&\s+)?{_FIRM_WORD}){{0,4}}"
_NAMED = re.compile(
    rf"(?:from|to|of)\s+(?:the\s+line\s+of\s+)?(?P<name>{_NAME})\s+(?:with|from|of|at)\s+"
    rf"(?P<firm>{_FIRM})"
    rf"|^\s*(?:[(\[]Operator Instructions[)\]]\s*)?(?P<name2>{_NAME}),\s+(?P<firm2>{_FIRM})\.",
)
_LOOK = 260  # characters after "Operator" searched for the analyst's name
# Further on, only an introduction that says it's a question: some operators read the queue
# instructions first (Cleveland-Cliffs: 528 characters before "Today's first question is coming
# from..."), and a wider search for any "of <name> at <firm>" also caught job titles ("Head of
# Investor Relations at TD SYNNEX").
_LATE_LOOK = 800
_NAMED_LATE = re.compile(
    rf"\bquestion\s+(?:[a-z]+\s+){{0,3}}?(?:from|to|of)\s+(?:the\s+line\s+of\s+)?"
    rf"(?P<name>{_NAME})\s+(?:with|from|of|at)\s+(?P<firm>{_FIRM})"
)

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATES = (  # "October 13, 2026", "OCTOBER 13, 2026", "13-Oct-2026", "2026-10-13"
    (re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", re.IGNORECASE), "%B %d, %Y"),
    (re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b"), "%d-%b-%Y"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "%Y-%m-%d"),
)
_HEADER_CHARS = 3_000  # where a transcript states its call's date


@dataclass(frozen=True)
class Exchange:
    analyst: str
    firm: str
    text: str


@dataclass(frozen=True)
class Transcript:
    remarks: str
    exchanges: list[Exchange]


def normalize(text: str) -> str:
    """Plain apostrophes and spaces, no dot leaders, page numbers or transcript-service footers,
    one space between words and no blank lines."""
    text = _BOILERPLATE.sub("", _DOT_LEADERS.sub(" ", text.translate(_PLAIN)))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def split(text: str) -> Transcript:
    """The prepared remarks and the Q&A exchanges of a normalized transcript. A call with no
    named questions (or a transcript in a format these rules don't know) has no exchanges."""
    starts: list[tuple[int, str, str]] = []
    turns = list(_OPERATOR.finditer(text))
    for i, match in enumerate(turns):
        # The operator's own words only: never into the next operator turn.
        turn_end = turns[i + 1].start() if i + 1 < len(turns) else len(text)
        window = text[match.end() : min(match.end() + _LOOK, turn_end)].replace("\n", " ")
        named = _NAMED.search(window)
        if named is None:
            late = text[match.end() : min(match.end() + _LATE_LOOK, turn_end)].replace("\n", " ")
            named = _NAMED_LATE.search(late)
        if named is None:
            continue
        groups = named.groupdict()  # the late pattern has no name2/firm2
        name = (groups.get("name") or groups.get("name2") or "").strip()
        firm = (groups.get("firm") or groups.get("firm2") or "").strip()
        if starts and starts[-1][1] == name:
            continue  # the operator named the same analyst again, for a follow-up
        starts.append((match.start(), name, firm))
    exchanges = [
        Exchange(
            name, firm, text[start : starts[i + 1][0] if i + 1 < len(starts) else None].strip()
        )
        for i, (start, name, firm) in enumerate(starts)
    ]
    return Transcript(text[: starts[0][0]].strip() if starts else text, exchanges)


def call_date(text: str) -> date | None:
    """The first date a transcript's header gives ("October 13, 2026", "13-Oct-2026")."""
    header = text[:_HEADER_CHARS]
    found: list[tuple[int, date]] = []
    for pattern, fmt in _DATES:
        for match in pattern.finditer(header):
            try:
                found.append((match.start(), datetime.strptime(match.group(0).title(), fmt).date()))
            except ValueError:  # "February 30, 2026"
                continue
    return min(found)[1] if found else None
