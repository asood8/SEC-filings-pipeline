"""Fetch the earnings-call transcripts companies publish on their own sites (ADR-0022).

Only the companies in `config/transcript_sources.toml` are read: those whose sites' terms allow
automated downloads and that post a transcript a plain page links to or an address the call's
date gives. A source either names a `listing` page and a `link` pattern for its transcripts, or
gives `urls` templates filled from a call's date. Call dates come from the earnings 8-Ks (item
2.02) discovery already stored, so nothing here calls SEC.

Requests are polite: a User-Agent that names the project, robots.txt honored for every request
and redirect, at least a second between requests to one site, and documents capped at 4 MB. Only
recent transcripts are fetched: calls of the last `lookback_days` for addresses built from dates
and dated links, and a listing's newest few (`keep`) otherwise.

The text goes to MongoDB, then a row to Postgres. The source URL is the natural key, and a
company has one transcript per call day, so a re-run (or a listing whose links moved) stores
nothing twice. Neither is ever served: the API serves only summaries and the link.
"""

import logging
import re
import time
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Self
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx2
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from filings_common import metrics, transcripts
from filings_common.db import CallTranscript, Company, Filing, Form8KFiling
from filings_common.mongo import TranscriptStore

logger = logging.getLogger(__name__)

MAX_BYTES = 4 * 1024 * 1024  # 3x the largest real transcript, a 1.3 MB PDF
MAX_REDIRECTS = 5
MIN_WORDS = 1_000  # less is a placeholder page, not a call's transcript
EARNINGS_ITEM = "2.02"
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_NOT_POSTED = frozenset({404, 410})


class SourceError(Exception):
    """A source can't be read this run (robots.txt bars it, a server error, a broken page)."""


# --- Sources --------------------------------------------------------------------------------------


class Source(BaseModel):
    """Where one company publishes its transcripts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    # A page that links every transcript, and a pattern for the links (matched against absolute
    # URLs). Named groups order them, newest first: `date` (YYYY-MM-DD), or `year` and `quarter`.
    listing: str | None = None
    link: re.Pattern[str] | None = None
    keep: int = Field(default=2, ge=1)  # how many of the newest links to consider
    # Or addresses filled from a call's date: {date:%Y-%m-%d}, and the quarter the call reports:
    # {year}, {yy}, {quarter}, {ordinal} ("3rd"), {fiscal_year}, {fiscal_quarter}.
    urls: list[str] = []
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    # An HTML page with more than the transcript: keep the text after `start`, before `end`.
    start: re.Pattern[str] | None = None
    end: re.Pattern[str] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if (self.listing is None) != (self.link is None):
            raise ValueError(f"{self.ticker}: a listing needs a link pattern, and the reverse")
        if (self.listing is None) == (not self.urls):
            raise ValueError(f"{self.ticker}: give either a listing or urls")
        return self


class _Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: list[Source] = Field(min_length=1)


def load_sources(path: Path) -> list[Source]:
    try:
        sources = _Sources.model_validate(tomllib.loads(path.read_text())).source
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ValueError(f"{path}: not a valid transcript source file: {exc}") from exc
    tickers = [s.ticker for s in sources]
    if len(set(tickers)) != len(tickers):
        raise ValueError(f"{path}: a ticker is listed twice")
    return sources


# --- The quarter a call reports -------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    year: int  # the calendar year and quarter that ended
    quarter: int
    fiscal_year: int  # named for the year it ends in, like most companies do
    fiscal_quarter: int


def _month_end(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def reported_period(call_day: date, fiscal_year_start_month: int = 1) -> Period:
    """The quarter a call on `call_day` reports: the last one that ended at least a week before."""
    cutoff = call_day - timedelta(days=7)
    quarter_ends = {(fiscal_year_start_month + 3 * k - 2) % 12 + 1 for k in range(1, 5)}
    year, month = cutoff.year, cutoff.month
    while month not in quarter_ends or _month_end(year, month) > cutoff:
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    start = fiscal_year_start_month
    fiscal_year = year if start == 1 or month < start else year + 1
    return Period(year, (month - 1) // 3 + 1, fiscal_year, (month - start) % 12 // 3 + 1)


def fill(template: str, call_day: date, fiscal_year_start_month: int = 1) -> str:
    p = reported_period(call_day, fiscal_year_start_month)
    return template.format(
        date=call_day,
        year=p.year,
        yy=f"{p.year % 100:02d}",
        quarter=p.quarter,
        ordinal=ORDINALS[p.quarter],
        fiscal_year=p.fiscal_year,
        fiscal_quarter=p.fiscal_quarter,
    )


# --- Reading pages and documents ------------------------------------------------------------------


@dataclass(frozen=True)
class _Reply:
    status: int
    location: str | None
    content_type: str
    body: bytes


@dataclass(frozen=True)
class Document:
    url: str  # after redirects
    content_type: str
    body: bytes


class SiteClient:
    """GETs from companies' sites (not SEC's: that is EdgarClient's job): robots.txt honored for
    every request and redirect, at least `delay` seconds between requests to one host."""

    def __init__(
        self,
        user_agent: str,
        *,
        delay: float = 1.0,
        timeout: float = 30.0,
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._agent = user_agent.split("/", 1)[0]  # the product token robots.txt rules name
        self._delay = delay
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._http = httpx2.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=False,  # each hop is checked against its site's robots.txt
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def get(self, url: str) -> Document | None:
        """The document at `url`, or None when the site says it isn't there (404, 410)."""
        for _ in range(MAX_REDIRECTS + 1):
            if not self._allowed(url):
                raise SourceError(f"robots.txt disallows {url}")
            reply = self._request(url)
            if reply.status in _REDIRECTS and reply.location:
                url = urljoin(url, reply.location)
                continue
            if reply.status in _NOT_POSTED:
                return None
            if reply.status != 200:
                raise SourceError(f"{url}: HTTP {reply.status}")
            return Document(url, reply.content_type, reply.body)
        raise SourceError(f"{url}: more than {MAX_REDIRECTS} redirects")

    def _request(self, url: str) -> "_Reply":
        host = urlsplit(url).netloc
        wait = self._last.get(host, float("-inf")) + self._delay - self._clock()
        if wait > 0:
            self._sleep(wait)
        try:
            with self._http.stream("GET", url) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise SourceError(f"{url}: larger than {MAX_BYTES} bytes")
                return _Reply(
                    response.status_code,
                    response.headers.get("location"),
                    response.headers.get("content-type", ""),
                    bytes(body),
                )
        except httpx2.HTTPError as exc:
            raise SourceError(f"{url}: {exc!r}") from exc
        finally:
            self._last[host] = self._clock()

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._read_robots(origin)
        robots = self._robots[origin]
        return robots is None or robots.can_fetch(self._agent, url)

    def _read_robots(self, origin: str) -> RobotFileParser | None:
        """None allows everything: no robots.txt (a 4xx, as RFC 9309 says). Redirects are
        followed (up to five, as it asks); a server error allows nothing this run."""
        url = f"{origin}/robots.txt"
        for _ in range(MAX_REDIRECTS + 1):
            reply = self._request(url)
            if reply.status in _REDIRECTS and reply.location:
                url = urljoin(url, reply.location)
                continue
            if 400 <= reply.status < 500:
                return None
            if reply.status != 200:
                raise SourceError(f"{url}: HTTP {reply.status}")
            robots = RobotFileParser()
            robots.parse(reply.body.decode("utf-8", errors="replace").splitlines())
            return robots
        raise SourceError(f"{origin}/robots.txt: more than {MAX_REDIRECTS} redirects")


class _Text(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "template", "svg"})
    _BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_text(page: str) -> str:
    parser = _Text()
    parser.feed(page)
    parser.close()
    return "".join(parser.parts)


def document_text(document: Document, source: Source) -> str | None:
    """A transcript document's text, normalized (filings_common.transcripts.normalize). None when
    the page doesn't hold the transcript yet (no `start` marker: a call page before it's posted)."""
    if document.body.startswith(b"%PDF") or "pdf" in document.content_type:
        try:
            reader = PdfReader(BytesIO(document.body))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except (PyPdfError, ValueError) as exc:
            raise SourceError(f"{document.url}: unreadable PDF: {exc}") from exc
    else:
        text = html_text(document.body.decode("utf-8", errors="replace"))
    if source.start is not None:
        if (begin := source.start.search(text)) is None:
            return None
        text = text[begin.end() :]
    if source.end is not None and (stop := source.end.search(text)) is not None:
        text = text[: stop.start()]
    return transcripts.normalize(text)


@dataclass(frozen=True)
class Candidate:
    url: str
    call_date: date | None  # when the address or link gives it


# Links in href attributes, and in the JSON some sites embed for their document lists (Q4's:
# {"text":"Q2","link":"//s205.q4cdn.com/.../Ford-Q2-2026-Earnings-Call-Transcript.pdf"}).
_LINKS = re.compile(
    r"""href\s*=\s*["'](?P<href>[^"']+)["']"""
    r'''|"(?:link|url|href)"\s*:\s*"(?P<json>[^"]+)"''',
    re.IGNORECASE,
)


def listing_candidates(page: str, base_url: str, source: Source) -> list[Candidate]:
    """The newest `source.keep` transcript links on a listing page."""
    assert source.link is not None
    found: dict[str, tuple[tuple[int, ...], date | None]] = {}
    for found_link in _LINKS.finditer(page):
        href = (found_link["href"] or found_link["json"]).replace("\\/", "/")
        url = urljoin(base_url, href.strip())
        if url in found or (match := source.link.search(url)) is None:
            continue
        groups = match.groupdict()
        day = date.fromisoformat(groups["date"]) if groups.get("date") else None
        order = (day.toordinal(),) if day else (int(groups["year"]), int(groups["quarter"]))
        found[url] = (order, day)
    newest = sorted(found.items(), key=lambda item: item[1][0], reverse=True)[: source.keep]
    return [Candidate(url, day) for url, (_, day) in newest]


def template_candidates(source: Source, call_days: Iterable[date]) -> list[Candidate]:
    """Every address the templates give for these call days, each once, with its earliest day
    (an address without a date is the same for the day of a release and the next)."""
    candidates: dict[str, Candidate] = {}
    for day in sorted(set(call_days)):
        for template in source.urls:
            url = fill(template, day, source.fiscal_year_start_month)
            candidates.setdefault(url, Candidate(url, day))
    return list(candidates.values())


# --- The job --------------------------------------------------------------------------------------


@dataclass
class FetchReport:
    stored: list[str] = field(default_factory=list)  # tickers, one per transcript
    failed: list[str] = field(default_factory=list)
    overdue: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Company:
    cik: int
    ticker: str


def earnings_days(session: Session, cik: int, since: date) -> list[date]:
    """Days a company may have held an earnings call since `since`: the filing day of each earnings
    8-K and the next (a call the morning after a release), and any call day an 8-K announced."""
    rows = session.execute(
        sa.select(Filing.filed_at, Form8KFiling.investor_call)
        .join(Form8KFiling, Form8KFiling.accession_no == Filing.accession_no)
        .where(
            Filing.cik == cik,
            Filing.filed_at >= since,
            Form8KFiling.items.contains([EARNINGS_ITEM]),
        )
    ).all()
    days: set[date] = set()
    for filed_at, call in rows:
        days |= {filed_at, filed_at + timedelta(days=1)}
        if call and call.get("day"):
            days.add(date.fromisoformat(call["day"]))
    return sorted(days)


def fetch_transcripts(
    sessions: sessionmaker[Session],
    store: TranscriptStore,
    client: SiteClient,
    sources: Sequence[Source],
    *,
    today: date,
    lookback_days: int,
    overdue_days: int,
) -> FetchReport:
    """Store the new transcripts of every source company on the watchlist."""
    report = FetchReport()
    since = today - timedelta(days=lookback_days)
    with sessions() as session:
        companies = {
            c.ticker: _Company(c.cik, c.ticker)
            for c in session.scalars(sa.select(Company).where(Company.on_watchlist))
        }
    for source in sources:
        company = companies.get(source.ticker)
        if company is None:
            logger.warning("transcript source %s is not on the watchlist", source.ticker)
            continue
        try:
            report.stored += _fetch_company(sessions, store, client, source, company, since, today)
        except SourceError as exc:
            logger.warning("transcripts of %s not read: %s", source.ticker, exc)
            report.failed.append(source.ticker)
        if _overdue(sessions, company, today, since, overdue_days):
            report.overdue.append(source.ticker)
    metrics.TRANSCRIPTS_OVERDUE_VALUES.replace("fetch-transcripts", [(len(report.overdue), {})])
    if report.overdue:
        logger.warning("no transcript yet for the latest earnings call of %s", report.overdue)
    return report


def _fetch_company(
    sessions: sessionmaker[Session],
    store: TranscriptStore,
    client: SiteClient,
    source: Source,
    company: _Company,
    since: date,
    today: date,
) -> list[str]:
    with sessions() as session:
        stored = session.execute(
            sa.select(CallTranscript.source_url, CallTranscript.call_date).where(
                CallTranscript.cik == company.cik
            )
        ).all()
        days = [] if source.listing else earnings_days(session, company.cik, since)
    known = {url for url, _ in stored}
    # A quarter whose transcript is stored is done: its other addresses aren't tried again.
    done = {reported_period(day, source.fiscal_year_start_month) for _, day in stored}
    if source.listing:
        listing = client.get(source.listing)
        if listing is None:
            raise SourceError(f"listing page {source.listing} not found")
        page = listing.body.decode("utf-8", errors="replace")
        candidates = listing_candidates(page, listing.url, source)
    else:
        candidates = template_candidates(source, (d for d in days if d <= today))
    new = []
    for candidate in candidates:
        if candidate.url in known:
            continue
        if candidate.call_date and (
            candidate.call_date < since
            or reported_period(candidate.call_date, source.fiscal_year_start_month) in done
        ):
            continue
        document = client.get(candidate.url)
        if document is None:
            continue  # not posted (yet)
        stored_day = _store(sessions, store, source, company, candidate, document, done, since)
        if stored_day is not None:
            known.add(candidate.url)
            done.add(reported_period(stored_day, source.fiscal_year_start_month))
            new.append(company.ticker)
    return new


def _store(
    sessions: sessionmaker[Session],
    store: TranscriptStore,
    source: Source,
    company: _Company,
    candidate: Candidate,
    document: Document,
    done: set[Period],
    since: date,
) -> date | None:
    text = document_text(document, source)
    if text is None:
        logger.info("%s: no transcript on the page yet", candidate.url)
        return None
    words = len(text.split())
    exchanges = len(transcripts.split(text).exchanges)
    stated = transcripts.call_date(text)
    # The date the transcript states, unless the address's date disagrees by more than a few
    # days (a header that dates something else).
    day = candidate.call_date
    if stated and (day is None or abs((stated - day).days) <= 3):
        day = stated
    if words < MIN_WORDS or exchanges == 0 or day is None:
        logger.warning(
            "%s: not a usable transcript (%d words, %d exchanges, call date %s)",
            candidate.url,
            words,
            exchanges,
            day,
        )
        return None
    if day < since:
        # A listing link dated only by its quarter is fetched before its date is known.
        logger.info("%s: the call of %s is older than the lookback; not stored", candidate.url, day)
        return None
    if reported_period(day, source.fiscal_year_start_month) in done:
        # The same call under another address (a listing's links moved): stored once.
        logger.info("%s: this call's transcript is already stored", candidate.url)
        return None
    store.upsert(
        source_url=candidate.url, cik=company.cik, ticker=company.ticker, call_date=day, text=text
    )
    with sessions.begin() as session:
        session.execute(
            pg_insert(CallTranscript)
            .values(
                cik=company.cik,
                call_date=day,
                source_url=candidate.url,
                text_sha256=sha256(text.encode()).hexdigest(),
                words=words,
                exchanges=exchanges,
            )
            .on_conflict_do_nothing()  # the source URL, or the company's call on that day
        )
    metrics.TRANSCRIPTS_STORED.add(1)
    logger.info("stored %s transcript of %s: %d exchanges", company.ticker, day, exchanges)
    return day


def _overdue(
    sessions: sessionmaker[Session], company: _Company, today: date, since: date, days: int
) -> bool:
    """Whether the company's latest earnings 8-K is at least `days` old and no transcript of a
    call within a few days of it is stored."""
    with sessions() as session:
        latest = session.scalar(
            sa.select(sa.func.max(Filing.filed_at))
            .join(Form8KFiling, Form8KFiling.accession_no == Filing.accession_no)
            .where(Filing.cik == company.cik, Form8KFiling.items.contains([EARNINGS_ITEM]))
        )
        if latest is None or latest < since or (today - latest).days < days:
            return False
        found = session.scalar(
            sa.select(sa.func.count())
            .select_from(CallTranscript)
            .where(
                CallTranscript.cik == company.cik,
                CallTranscript.call_date.between(
                    latest - timedelta(days=1), latest + timedelta(days=3)
                ),
            )
        )
    return not found
