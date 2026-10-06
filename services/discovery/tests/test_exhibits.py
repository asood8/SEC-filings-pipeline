from pathlib import Path

import httpx2
import pytest

from filings_common.config import EdgarSettings
from filings_common.edgar import EdgarClient
from filings_common.storage import LocalObjectStore
from filings_discovery.exhibits import (
    DocumentRef,
    download_exhibits,
    ex99_exhibits,
    index_headers_url,
    parse_index_headers,
)

# Real EDGAR index-headers page of Adobe's Q3 2026 earnings 8-K.
INDEX_HEADERS = (
    Path(__file__).parents[3]
    / "tests/fixtures/index_headers/0000796343-26-000147-index-headers.html"
)
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/796343/000079634326000147"


class NoLimit:
    def acquire(self) -> float:
        return 0.0


def test_parses_every_document_of_a_real_filing() -> None:
    documents = parse_index_headers(INDEX_HEADERS.read_text())
    assert len(documents) == 14
    assert documents[0] == DocumentRef("8-K", 1, "adbe-20260910.htm", "8-K")
    assert documents[1] == DocumentRef("EX-99.1", 2, "adbeex991q326.htm", "EX-99.1")
    assert ex99_exhibits(documents) == [documents[1]]


def test_keeps_only_ex99_exhibits_in_html_or_text() -> None:
    documents = [
        DocumentRef("EX-99.1", 2, "press.htm", None),
        DocumentRef("EX-99.2", 3, "slides.pdf", None),
        DocumentRef("EX-10.1", 4, "agreement.htm", None),
        DocumentRef("EX-101.SCH", 5, "x.xsd", None),
        DocumentRef("EX-99", 6, "letter.txt", None),
        DocumentRef("GRAPHIC", 7, "logo.jpg", None),
    ]
    assert [d.filename for d in ex99_exhibits(documents)] == ["press.htm", "letter.txt"]


def edgar(routes: dict[str, httpx2.Response], requested: list[str]) -> EdgarClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        return routes.get(str(request.url), httpx2.Response(404))

    settings = EdgarSettings(_env_file=None, contact_email="ops@example.com", max_attempts=1)
    return EdgarClient(settings, NoLimit(), transport=httpx2.MockTransport(handler))


def test_downloads_only_ex99_exhibits(tmp_path: Path) -> None:
    requested: list[str] = []
    routes = {
        index_headers_url(796343, "0000796343-26-000147"): httpx2.Response(
            200, content=INDEX_HEADERS.read_bytes()
        ),
        f"{ARCHIVE}/adbeex991q326.htm": httpx2.Response(200, content=b"<p>Record Q3</p>"),
    }
    store = LocalObjectStore(tmp_path)
    with edgar(routes, requested) as client:
        downloaded = download_exhibits(
            client, store, 796343, "0000796343-26-000147", "filings/796343/0000796343-26-000147"
        )
    [exhibit] = downloaded
    assert exhibit.object_key == "filings/796343/0000796343-26-000147/adbeex991q326.htm"
    assert store.get(exhibit.object_key) == b"<p>Record Q3</p>"
    assert requested == list(routes)  # nothing else: no contracts, XBRL, or images


def test_no_index_headers_page_means_no_exhibits(tmp_path: Path) -> None:
    with edgar({}, []) as client:
        assert (
            download_exhibits(client, LocalObjectStore(tmp_path), 1, "0000000001-26-000001", "p")
            == []
        )


def test_other_errors_are_not_swallowed(tmp_path: Path) -> None:
    routes = {index_headers_url(1, "0000000001-26-000001"): httpx2.Response(500)}
    with edgar(routes, []) as client, pytest.raises(httpx2.HTTPStatusError):
        download_exhibits(client, LocalObjectStore(tmp_path), 1, "0000000001-26-000001", "p")
