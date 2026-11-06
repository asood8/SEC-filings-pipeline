"""8-K parser: split a current report into its numbered items (2.02 results, 5.02 officers...).

8-Ks are HTML, nowadays inline XBRL. The document is converted to plain-text lines, and each line
that starts with "Item N.NN" begins an item. References inside sentences ("the information in
Item 1.01 above") are not at the start of a line, so they don't split the text. Everything before
the first item is the cover page and is dropped, as is everything from the signature block on.

Some filing agents add a table of contents: item headings with page numbers, then "Signatures".
Page numbers are dropped, so a table of contents reads as a run of headings with no text, and the
parser takes the first run that has any.

An 8-K/A is parsed the same way, and keeps two things more: the explanatory note some open with
(on the cover page, before the items), and the filing date of the 8-K it amends, from its text.
"""

import re
from collections.abc import Iterator, Sequence
from datetime import date
from html.parser import HTMLParser
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from filings_common.db import Filing, Form8KFiling
from filings_common.parsed import EASTERN, ITEM_TITLES, Exhibit, Form8K, Item
from filings_parser import amendments
from filings_parser.calls import find_call
from filings_parser.documents import RawExhibit

# 2: adds the text of EX-99 exhibits (press releases) to the payload.
PARSER_VERSION = 2
# Exhibit text kept per exhibit. Summaries use far less; this bounds MongoDB storage.
MAX_EXHIBIT_TEXT = 100_000
MAX_NOTE_TEXT = 5_000

# The title may follow the number with no space: ADM sets "Item 2.02" and "Results of Operations"
# in adjacent spans, which read "Item 2.02Results of Operations".
_ITEM_HEADING = re.compile(
    r"^item\s*(\d{1,2})\.(\d{2})(?!\d)[\s.:\-\u2013\u2014]*(.*)$", re.IGNORECASE
)
_SIGNATURES = re.compile(r"^signatures?\b", re.IGNORECASE)
# Lines that are only layout: page numbers ("3", "- 3 -") and "Table of Contents" back-links.
_LAYOUT_LINE = re.compile(
    r"^(?:[-\u2013\u2014\s]*\d{1,3}[-\u2013\u2014\s]*|table of contents)$", re.I
)
_EXPLANATORY_NOTE = re.compile(r"^explanatory note\b[\s.:\-\u2013\u2014]*(.*)$", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# "...the Current Report on Form 8-K filed with the SEC on November 14, 2025", "...Form 8-K that
# the Company filed on February 10, 2026": a Form 8-K (not 8-K/A), then "filed" or "furnished",
# then "on <date>", close together.
_AMENDED_8K = re.compile(
    r"\bform\s+8-k\b(?!\s*/\s*a)"
    r".{0,200}?\b(?:filed|furnished)\b.{0,120}?\bon\s+"
    rf"({'|'.join(_MONTHS)})[a-z]*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})",
    re.IGNORECASE | re.DOTALL,
)

_BLOCK_TAGS = frozenset(
    [
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    ]
)
_CELL_TAGS = frozenset({"td", "th"})
_HIDDEN_TAGS = frozenset({"script", "style", "head", "title", "ix:header"})
_VOID_TAGS = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    ]
)


class Form8KParseError(ValueError):
    pass


class _TextExtractor(HTMLParser):
    """HTML to text lines: block elements end lines, and hidden content is skipped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[bool] = []  # one entry per open element: is it hidden?

    def _hidden(self) -> bool:
        return any(self._open)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS or tag in _CELL_TAGS:
            self.parts.append("\n" if tag in _BLOCK_TAGS else " ")
        if tag in _VOID_TAGS:
            return
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        self._open.append(tag in _HIDDEN_TAGS or "display:none" in style)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or not self._open:
            return
        self._open.pop()
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden():
            self.parts.append(data)


def html_to_lines(document: str) -> list[str]:
    extractor = _TextExtractor()
    extractor.feed(document)
    extractor.close()
    text = "".join(extractor.parts).replace("\xa0", " ")
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.split("\n"))
    return [line for line in lines if line]


_Run = tuple[dict[str, list[str]], dict[str, str]]


def _item_runs(lines: list[str]) -> Iterator[_Run]:
    """Each run of item sections up to a signatures line: (text lines, heading) by item number."""
    items: dict[str, list[str]] = {}
    headings: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        if current is not None and _SIGNATURES.match(line):
            yield items, headings
            items, headings, current = {}, {}, None
            continue
        heading = _ITEM_HEADING.match(line)
        if heading:
            current = f"{int(heading[1])}.{heading[2]}"
            items.setdefault(current, [])
            headings.setdefault(current, heading[3].rstrip(". "))
        elif current is not None:
            items[current].append(line)
    if items:
        yield items, headings


def _content_lines(raw: bytes) -> list[str]:
    lines = html_to_lines(raw.decode("utf-8", errors="replace"))
    return [line for line in lines if not _LAYOUT_LINE.match(line)]


def parse_form8k(raw: bytes) -> Form8K:
    return _items(_content_lines(raw))


def _items(content: list[str]) -> Form8K:
    for items, headings in _item_runs(content):
        if any(items.values()):  # skips a table of contents (headings without text)
            return Form8K(
                items=[
                    Item(
                        number=number,
                        title=ITEM_TITLES.get(number) or headings[number] or f"Item {number}",
                        text="\n".join(text),
                    )
                    for number, text in items.items()
                ]
            )
    raise Form8KParseError("no 'Item N.NN' sections with text found")


def exhibit_text(exhibit: RawExhibit) -> str:
    raw = exhibit.content.decode("utf-8", errors="replace")
    if exhibit.filename.lower().endswith(".txt"):
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
    else:
        lines = html_to_lines(raw)
    # Exhibit files often start with SEC's packaging: type, sequence number, file name.
    packaging = {
        exhibit.type.upper(),
        exhibit.filename.upper(),
        (exhibit.description or "").upper(),
    }
    while lines and (lines[0].upper() in packaging or lines[0].isdigit()):
        lines.pop(0)
    return "\n".join(lines)[:MAX_EXHIBIT_TEXT]


def explanatory_note(content: list[str]) -> str | None:
    """The text under an "Explanatory Note" heading, up to the first item."""
    note: list[str] | None = None
    for line in content:
        if note is None:
            if heading := _EXPLANATORY_NOTE.match(line):
                note = [heading[1]] if heading[1] else []
            continue
        if _ITEM_HEADING.match(line) or _SIGNATURES.match(line):
            break
        note.append(line)
    text = "\n".join(note or [])[:MAX_NOTE_TEXT]
    return text or None


def amended_filing_date(form: Form8K) -> date | None:
    """The filing date of the 8-K an 8-K/A amends, from the first place its note or items
    give one; None when they don't."""
    texts = [form.explanatory_note or "", *(item.text for item in form.items)]
    for text in texts:
        for found in _AMENDED_8K.finditer(text):
            month, day, year = found.groups()
            try:
                return date(int(year), _MONTHS.index(month[:3].lower()) + 1, int(day))
            except ValueError:  # "February 30": not a date, keep looking
                continue
    return None


def _with_exhibits(form: Form8K, exhibits: Sequence[RawExhibit]) -> Form8K:
    form.exhibits = [
        Exhibit(type=e.type, description=e.description, text=exhibit_text(e)) for e in exhibits
    ]
    return form


def parse(raw: bytes, exhibits: Sequence[RawExhibit] = ()) -> dict[str, Any]:
    """The JSON-ready payload stored in MongoDB: the items, plus the text of each exhibit."""
    return _with_exhibits(parse_form8k(raw), exhibits).model_dump(mode="json")


def parse_amendment(raw: bytes, exhibits: Sequence[RawExhibit] = ()) -> dict[str, Any]:
    """An 8-K/A's payload: an 8-K's, plus its explanatory note and the date of the 8-K it
    amends."""
    content = _content_lines(raw)
    form = _with_exhibits(_items(content), exhibits)
    form.explanatory_note = explanatory_note(content)
    form.amends_filed_at = amended_filing_date(form)
    return form.model_dump(mode="json")


def save_key_fields(session: Session, accession_no: str, payload: dict[str, Any]) -> None:
    """Upsert the 8-K's item numbers and the call it announces into Postgres (`form8k_filings`),
    then link an 8-K/A to its 8-K, or an 8-K to amendments already saved (amendments module).
    "Today" in the filing is the day SEC accepted it, in Eastern time (its filing date is the
    next business day when accepted after 5:30 p.m.)."""
    form = Form8K.model_validate(payload)
    numbers = [item.number for item in form.items]
    filing = session.get(Filing, accession_no)
    call = None
    if filing is not None:
        amendments.lock_company(session, "8-K", filing.cik)
        accepted = filing.accepted_at
        call = find_call(form, accepted.astimezone(EASTERN).date() if accepted else filing.filed_at)
    values = {
        "items": numbers,
        # SQL NULL, not JSON null, when there is none.
        "investor_call": call.model_dump(mode="json") if call is not None else sa.null(),
    }
    stmt = pg_insert(Form8KFiling).values(accession_no=accession_no, **values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Form8KFiling.accession_no],
            set_={**values, "updated_at": sa.func.now()},
        )
    )
    amendments.form8k_saved(session, accession_no, form.amends_filed_at)
