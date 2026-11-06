"""Fetching companies' own earnings-call transcripts (ADR-0022), against a fake website.

Sites' pages and transcripts are written for these tests: their terms don't allow redistributing
the real ones."""

import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from pymongo.database import Database
from sqlalchemy.engine import Engine

from filings_common.db import (
    CallTranscript,
    Company,
    Filing,
    FilingStatus,
    Form8KFiling,
    session_factory,
)
from filings_common.mongo import TranscriptStore
from filings_discovery.transcripts import (
    MAX_BYTES,
    Candidate,
    Document,
    Period,
    SiteClient,
    Source,
    SourceError,
    document_text,
    fetch_transcripts,
    fill,
    listing_candidates,
    load_sources,
    reported_period,
    template_candidates,
)

CONFIG = Path(__file__).parents[3] / "config" / "transcript_sources.toml"
AGENT = "filings-pipeline/1.0 (+https://example.org)"


def pdf(lines: list[str]) -> bytes:
    """A one-page PDF showing `lines`, for pypdf to read back."""
    shown = " ".join(
        "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") '"
        for line in lines
    )
    content = f"BT /F1 9 Tf 40 780 Td 11 TL {shown} ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        " /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out + trailer.encode()


def call_lines(company: str = "Example Corp", day: str = "October 13, 2026") -> list[str]:
    """A transcript long enough to keep: remarks, then two analysts' questions."""
    remarks = [f"{company} third quarter earnings call, {day}."]
    remarks += ["Operator: Welcome to the call."]
    remarks += ["We grew revenue and kept costs flat this quarter, as planned. " * 2] * 60
    return [
        *remarks,
        "Operator: Our first question comes from John Smith with Big Bank. Your line is open.",
        "John Smith: How are deposits?",
        "Jane Roe: Deposits grew 3%.",
        "Operator: Our next question comes from Mary Major with Other Bank.",
        "Mary Major: And costs?",
        "Jane Roe: Flat.",
    ]


# --- Sources --------------------------------------------------------------------------------------


def test_the_source_file_is_valid() -> None:
    sources = load_sources(CONFIG)
    assert {s.ticker for s in sources} == {
        *("MSFT", "GOOGL", "JPM", "BAC", "JNJ", "PEP", "NKE"),  # the first survey
        *("F", "DELL", "SNX"),  # the second
    }


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[[source]]\nticker = "X"\n', "either a listing or urls"),
        ('[[source]]\nticker = "X"\nlisting = "https://x.example/"\n', "link pattern"),
        (
            '[[source]]\nticker = "X"\nurls = ["u"]\nlisting = "https://x.example/"\nlink = "a"\n',
            "either a listing or urls",
        ),
        (
            '[[source]]\nticker = "X"\nurls = ["u"]\n[[source]]\nticker = "X"\nurls = ["v"]\n',
            "listed twice",
        ),
        ('[[source]]\nticker = "X"\nurls = ["u"]\nstart = "("\n', "not a valid"),
        ("source = []\n", "not a valid"),
    ],
)
def test_bad_source_files_are_refused(tmp_path: Path, toml: str, message: str) -> None:
    path = tmp_path / "sources.toml"
    path.write_text(toml)
    with pytest.raises(ValueError, match=message):
        load_sources(path)


@pytest.mark.parametrize(
    ("call_day", "start", "period"),
    [
        (date(2026, 10, 13), 1, Period(2026, 3, 2026, 3)),  # JPM: Q3, reported in October
        (date(2026, 1, 13), 1, Period(2025, 4, 2025, 4)),  # Q4 of the year before
        (date(2026, 10, 8), 1, Period(2026, 3, 2026, 3)),
        (date(2026, 10, 3), 1, Period(2026, 2, 2026, 2)),  # under a week after Q3 ended
        (date(2026, 10, 28), 7, Period(2026, 3, 2027, 1)),  # Microsoft: fiscal 2027 Q1
        (date(2026, 1, 28), 7, Period(2025, 4, 2026, 2)),
        (date(2026, 4, 29), 7, Period(2026, 1, 2026, 3)),
        (date(2026, 7, 29), 7, Period(2026, 2, 2026, 4)),
        (date(2026, 10, 1), 6, Period(2026, 3, 2027, 1)),  # Nike: fiscal 2027 Q1 ended August
    ],
)
def test_the_reported_quarter_is_the_last_that_ended_a_week_before(
    call_day: date, start: int, period: Period
) -> None:
    assert reported_period(call_day, start) == period


def test_templates_are_filled_from_the_call_day() -> None:
    template = (
        "{date:%Y}/{date:%b}/{date:%d}/{year}_Q{quarter}-{ordinal}-{yy}"
        "-fy{fiscal_year}q{fiscal_quarter}"
    )
    assert fill(template, date(2026, 10, 28), 7) == "2026/Oct/28/2026_Q3-3rd-26-fy2027q1"


def test_each_address_is_tried_once_with_its_earliest_day() -> None:
    source = Source(
        ticker="X",
        urls=["https://x.example/{year}q{quarter}.pdf", "https://x.example/{date:%Y-%m-%d}.pdf"],
    )
    candidates = template_candidates(source, [date(2026, 10, 14), date(2026, 10, 13)])
    assert candidates == [
        Candidate("https://x.example/2026q3.pdf", date(2026, 10, 13)),
        Candidate("https://x.example/2026-10-13.pdf", date(2026, 10, 13)),
        Candidate("https://x.example/2026-10-14.pdf", date(2026, 10, 14)),
    ]


LISTING = """
<a href="/docs/q2-2026-transcript.pdf">Q2</a> <a href='/docs/q4-2025-transcript.pdf'>Q4</a>
<a href="https://cdn.example/docs/q3-2026-transcript.pdf">Q3</a>
<a href="/docs/q3-2026-slides.pdf">slides</a> <a href="/docs/q3-2026-transcript.pdf\\">escaped</a>
<a href="/docs/2026-10-14+Call.pdf">dated</a> <a href="/docs/2026-07-14+Call.pdf">dated</a>
"""


def test_a_listing_gives_its_newest_transcript_links() -> None:
    by_quarter = Source(
        ticker="X",
        listing="https://x.example/earnings",
        link=r"/docs/q(?P<quarter>[1-4])-(?P<year>\d{4})-transcript\.pdf$",
    )
    assert listing_candidates(LISTING, "https://x.example/earnings", by_quarter) == [
        Candidate("https://cdn.example/docs/q3-2026-transcript.pdf", None),
        Candidate("https://x.example/docs/q2-2026-transcript.pdf", None),
    ]
    by_date = by_quarter.model_copy(
        update={"link": re.compile(r"/docs/(?P<date>[\d-]{10})\+Call\.pdf$"), "keep": 1}
    )
    assert listing_candidates(LISTING, "https://x.example/earnings", by_date) == [
        Candidate("https://x.example/docs/2026-10-14+Call.pdf", date(2026, 10, 14))
    ]


def test_a_listing_reads_links_from_the_json_a_page_embeds() -> None:
    page = (
        'documentCategoryData: [{"category":"Earnings Transcript","data":[{"year":2026,'
        '"documents":[{"text":"Q1","link":"//cdn.example/files/2026/q1/X-Q1-2026-Transcript.pdf"},'
        '{"text":"Q2","link":"\\/\\/cdn.example\\/files\\/2026\\/q2\\/X-Q2-2026-Transcript.pdf"},'
        '{"text":"Q3","link":"//cdn.example/files/X-Q3-Transcript.pdf"}]}]}]'
    )
    source = Source(
        ticker="X",
        listing="https://x.example/financials",
        link=r"files/(?P<year>\d{4})/q(?P<quarter>[1-4])/X-Q[1-4]-\d{4}-Transcript\.pdf$",
    )
    assert listing_candidates(page, "https://x.example/financials", source) == [
        Candidate("https://cdn.example/files/2026/q2/X-Q2-2026-Transcript.pdf", None),
        Candidate("https://cdn.example/files/2026/q1/X-Q1-2026-Transcript.pdf", None),
    ]  # not the Q3 file, whose name gives no year


# --- Reading sites --------------------------------------------------------------------------------


class FakeSite:
    """Pages by URL; anything else is a 404. Records each request and the waits."""

    def __init__(self, pages: dict[str, httpx2.Response]) -> None:
        self.pages = pages
        self.requests: list[str] = []
        self.waits: list[float] = []
        self.now = 0.0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(str(request.url))
        assert request.headers["user-agent"] == AGENT
        return self.pages.get(str(request.url), httpx2.Response(404))

    def client(self) -> SiteClient:
        def sleep(seconds: float) -> None:
            self.waits.append(seconds)
            self.now += seconds

        return SiteClient(
            AGENT, transport=httpx2.MockTransport(self.handler), sleep=sleep, clock=lambda: self.now
        )


def ok(body: bytes | str, content_type: str = "text/html") -> httpx2.Response:
    return httpx2.Response(
        200,
        content=body.encode() if isinstance(body, str) else body,
        headers={"content-type": content_type},
    )


def test_robots_txt_is_honored_and_requests_are_spaced() -> None:
    site = FakeSite(
        {
            "https://a.example/robots.txt": ok(
                "User-agent: filings-pipeline\nDisallow: /private/\n"
            ),
            "https://a.example/one": ok("one"),
            "https://a.example/two": ok("two"),
        }
    )
    with site.client() as client:
        assert client.get("https://a.example/one") == Document(
            "https://a.example/one", "text/html", b"one"
        )
        assert client.get("https://a.example/two") is not None
        assert client.get("https://a.example/missing") is None  # a 404: not posted yet
        with pytest.raises(SourceError, match=r"robots\.txt disallows"):
            client.get("https://a.example/private/x")
    assert site.requests.count("https://a.example/robots.txt") == 1
    assert site.waits == [1.0, 1.0, 1.0]  # a second between each of the four requests


def test_a_site_without_robots_txt_allows_everything_and_a_broken_one_nothing() -> None:
    site = FakeSite(
        {
            "https://a.example/x": ok("x"),
            "https://b.example/robots.txt": httpx2.Response(503),
        }
    )
    with site.client() as client:
        assert client.get("https://a.example/x") is not None
        with pytest.raises(SourceError, match="503"):
            client.get("https://b.example/x")


def test_a_redirected_robots_txt_is_followed() -> None:
    site = FakeSite(
        {
            "https://cdn.example/robots.txt": httpx2.Response(
                301, headers={"location": "https://www.example/robots.txt"}
            ),
            "https://www.example/robots.txt": ok("User-agent: *\nDisallow: /private/\n"),
            "https://cdn.example/doc.pdf": ok(b"%PDF", "application/pdf"),
        }
    )
    with site.client() as client:
        assert client.get("https://cdn.example/doc.pdf") is not None
        with pytest.raises(SourceError, match="disallows"):
            client.get("https://cdn.example/private/x")


def test_redirects_are_followed_checking_each_sites_robots_txt() -> None:
    site = FakeSite(
        {
            "https://a.example/go": httpx2.Response(
                302, headers={"location": "https://b.example/doc"}
            ),
            "https://b.example/robots.txt": ok("User-agent: *\nDisallow: /doc\n"),
            "https://a.example/hop": httpx2.Response(301, headers={"location": "/there"}),
            "https://a.example/there": ok("there"),
        }
    )
    with site.client() as client:
        with pytest.raises(SourceError, match=r"disallows https://b\.example/doc"):
            client.get("https://a.example/go")
        document = client.get("https://a.example/hop")
    assert document is not None
    assert document.url == "https://a.example/there"


def test_server_errors_and_oversized_documents_fail_the_source() -> None:
    site = FakeSite(
        {
            "https://a.example/error": httpx2.Response(500),
            "https://a.example/huge": ok(b"x" * (MAX_BYTES + 1), "application/pdf"),
        }
    )
    with site.client() as client:
        with pytest.raises(SourceError, match="HTTP 500"):
            client.get("https://a.example/error")
        with pytest.raises(SourceError, match="larger than"):
            client.get("https://a.example/huge")


def test_text_comes_from_pdfs_and_from_the_marked_part_of_a_page() -> None:
    source = Source(ticker="X", urls=["u"])
    text = document_text(
        Document("u", "application/pdf", pdf(["First line", "Second line"])), source
    )
    assert text == "First line\nSecond line"

    page = (
        "<html><nav>Menu</nav><script>var x = 1;</script><h2>Transcript</h2>"
        "<p>JANE ROE: Hello &amp; welcome.</p><p>END</p><footer>Follow us</footer></html>"
    )
    marked = Source(ticker="X", urls=["u"], start=r"(?m)^Transcript$", end=r"(?m)^END$")
    assert document_text(Document("u", "text/html", page.encode()), marked) == (
        "JANE ROE: Hello & welcome."
    )
    early = "<html><h2>Webcast</h2><p>The transcript will be posted.</p></html>"
    assert document_text(Document("u", "text/html", early.encode()), marked) is None
    with pytest.raises(SourceError, match="unreadable PDF"):
        document_text(Document("u", "application/pdf", b"%PDF-1.4 broken"), source)


# --- The job --------------------------------------------------------------------------------------

ACME_CIK, OTHER_CIK = 1001, 1002
TODAY = date(2026, 11, 4)


def seed(engine: Engine, *earnings: tuple[int, str, date]) -> None:
    with session_factory(engine).begin() as session:
        session.add_all(
            [
                Company(cik=ACME_CIK, ticker="ACME", name="Acme Corp", on_watchlist=True),
                Company(cik=OTHER_CIK, ticker="OTHR", name="Other Corp", on_watchlist=True),
            ]
        )
        session.flush()
        for cik, accession_no, filed_at in earnings:
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=cik,
                    form_type="8-K",
                    filed_at=filed_at,
                    primary_doc_url="https://www.sec.gov/x",
                    raw_object_key="raw/x",
                    status=FilingStatus.PARSED,
                )
            )
            session.flush()
            session.add(Form8KFiling(accession_no=accession_no, items=["2.02", "9.01"]))


def run(
    engine: Engine,
    mongo_db: Database[dict[str, Any]],
    site: FakeSite,
    sources: list[Source],
    today: date = TODAY,
) -> Any:
    with site.client() as client:
        return fetch_transcripts(
            session_factory(engine),
            TranscriptStore(mongo_db),
            client,
            sources,
            today=today,
            lookback_days=120,
            overdue_days=21,
        )


ACME = Source(
    ticker="ACME",
    urls=["https://acme.example/{year}q{quarter}.pdf", "https://acme.example/{date:%Y-%m-%d}.pdf"],
)
OTHER = Source(
    ticker="OTHR",
    listing="https://other.example/earnings",
    link=r"/q(?P<quarter>[1-4])-(?P<year>\d{4})\.pdf$",
)


def stored(engine: Engine) -> list[tuple[str, date, int]]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                CallTranscript.source_url, CallTranscript.call_date, CallTranscript.exchanges
            ).order_by(CallTranscript.id)
        )
        return [(url, day, exchanges) for url, day, exchanges in rows]


@pytest.mark.integration
def test_new_transcripts_are_stored_once_and_finished_quarters_not_tried_again(
    clean_db: Engine, mongo_db: Database[dict[str, Any]], measured: Any
) -> None:
    seed(
        clean_db,
        (ACME_CIK, "0000001001-26-000001", date(2026, 10, 13)),
        (OTHER_CIK, "0000001002-26-000001", date(2026, 10, 8)),
    )
    site = FakeSite(
        {
            "https://acme.example/2026-10-14.pdf": ok(pdf(call_lines()), "application/pdf"),
            "https://other.example/earnings": ok(
                '<a href="/q3-2026.pdf">Q3</a> <a href="/q2-2026.pdf">'
            ),
            "https://other.example/q3-2026.pdf": ok(
                pdf(call_lines("Other", "October 8, 2026")), "application/pdf"
            ),
        }
    )
    report = run(clean_db, mongo_db, site, [ACME, OTHER])

    assert report.stored == ["ACME", "OTHR"]
    assert report.failed == []
    assert stored(clean_db) == [
        ("https://acme.example/2026-10-14.pdf", date(2026, 10, 13), 2),  # the date it states
        ("https://other.example/q3-2026.pdf", date(2026, 10, 8), 2),
    ]
    text = TranscriptStore(mongo_db).text("https://acme.example/2026-10-14.pdf")
    assert text is not None
    assert "Our first question comes from John Smith" in text
    assert measured.total("filings.transcripts.stored") == 2

    site.requests.clear()
    again = run(clean_db, mongo_db, site, [ACME, OTHER])
    assert again.stored == []
    assert not any(url.startswith("https://acme.example/2026") for url in site.requests)
    assert "https://other.example/q3-2026.pdf" not in site.requests
    assert len(stored(clean_db)) == 2


@pytest.mark.integration
def test_unusable_documents_old_calls_and_broken_sources(
    clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    seed(
        clean_db,
        (ACME_CIK, "0000001001-26-000001", date(2026, 10, 13)),
        (ACME_CIK, "0000001001-26-000002", date(2026, 3, 1)),
    )  # before the lookback
    site = FakeSite(
        {
            "https://acme.example/2026q3.pdf": ok(
                pdf(["Prepared remarks will be posted."]), "application/pdf"
            ),
            "https://other.example/robots.txt": httpx2.Response(500),
        }
    )
    report = run(clean_db, mongo_db, site, [ACME, OTHER])

    assert report.stored == []
    assert report.failed == ["OTHR"]  # one broken site stops no other
    assert stored(clean_db) == []  # too short, no questions: not a transcript yet
    assert not any("2025q4" in url or "2026-03" in url for url in site.requests)


@pytest.mark.integration
def test_a_listed_transcript_older_than_the_lookback_is_not_stored(
    clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    seed(clean_db)
    site = FakeSite(
        {
            "https://other.example/earnings": ok(
                '<a href="/q3-2026.pdf">Q3</a> <a href="/q1-2026.pdf">Q1</a>'
            ),
            "https://other.example/q3-2026.pdf": ok(
                pdf(call_lines("Other", "October 8, 2026")), "application/pdf"
            ),
            "https://other.example/q1-2026.pdf": ok(
                pdf(call_lines("Other", "April 29, 2026")), "application/pdf"
            ),
        }
    )
    report = run(clean_db, mongo_db, site, [OTHER])
    assert report.stored == ["OTHR"]
    # The links give only quarters; April's call is past the 120 days once its text is read.
    assert [day for _, day, _ in stored(clean_db)] == [date(2026, 10, 8)]


@pytest.mark.integration
def test_an_earnings_call_with_no_transcript_three_weeks_on_is_overdue(
    clean_db: Engine, mongo_db: Database[dict[str, Any]], measured: Any
) -> None:
    seed(
        clean_db,
        (ACME_CIK, "0000001001-26-000001", date(2026, 10, 13)),
        (OTHER_CIK, "0000001002-26-000001", date(2026, 10, 30)),
    )  # too recent to be overdue
    site = FakeSite({"https://other.example/earnings": ok("<p>No links</p>")})
    report = run(clean_db, mongo_db, site, [ACME, OTHER])
    assert report.overdue == ["ACME"]
    assert measured.gauge("filings.transcripts.overdue") == {"": 1}


@pytest.mark.integration
def test_a_call_whose_link_moved_is_not_stored_twice(
    clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    seed(clean_db)
    transcript = ok(pdf(call_lines("Other", "October 8, 2026")), "application/pdf")
    site = FakeSite(
        {
            "https://other.example/earnings": ok('<a href="/q3-2026.pdf">Q3</a>'),
            "https://other.example/q3-2026.pdf": transcript,
        }
    )
    run(clean_db, mongo_db, site, [OTHER])
    site.pages["https://other.example/earnings"] = ok('<a href="/files/v2/q3-2026.pdf">Q3</a>')
    site.pages["https://other.example/files/v2/q3-2026.pdf"] = transcript
    report = run(clean_db, mongo_db, site, [OTHER])
    assert report.stored == []
    assert [url for url, _, _ in stored(clean_db)] == ["https://other.example/q3-2026.pdf"]
