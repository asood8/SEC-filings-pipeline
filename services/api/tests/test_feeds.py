import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api import atom
from filings_common.db import Filing, Summary, session_factory

pytestmark = pytest.mark.integration

NS = {"a": atom.ATOM_NS}


def parse(response: Any) -> ET.Element:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/atom+xml; charset=utf-8"
    return ET.fromstring(response.content)


def titles(feed: ET.Element) -> list[str]:
    return [
        entry.findtext("a:title", namespaces=NS) or "" for entry in feed.iterfind("a:entry", NS)
    ]


def test_summarized_filings_newest_first(client: TestClient, seeded: Engine, ids: Any) -> None:
    with session_factory(seeded).begin() as session:  # discovered, not summarized yet: left out
        session.add(
            Filing(
                accession_no="0000320193-26-000099",
                cik=320193,
                form_type="8-K",
                filed_at=date(2026, 10, 1),
                primary_doc_url="https://www.sec.gov/Archives/0000320193-26-000099.htm",
                raw_object_key="filings/320193/0000320193-26-000099/doc",
            )
        )
    feed = parse(client.get("/feeds/filings.atom"))
    assert feed.findtext("a:title", namespaces=NS) == "SEC filings"
    assert titles(feed) == [
        "AAPL Form 4: Tim Cook sold 50,000 AAPL shares",
        "MSFT 8-K: Microsoft reports record quarterly revenue",
        "AAPL 8-K: Apple names Kevan Parekh chief financial officer",
    ]
    entry = feed.findall("a:entry", NS)[1]
    url = f"https://www.sec.gov/Archives/{ids.msft_8k}.htm"
    assert entry.findtext("a:id", namespaces=NS) == url
    assert entry.find("a:link", NS).get("href") == url  # type: ignore[union-attr]
    assert entry.findtext("a:author/a:name", namespaces=NS) == "Microsoft Corp"
    assert entry.find("a:category", NS).get("term") == "8-K"  # type: ignore[union-attr]
    assert entry.findtext("a:published", namespaces=NS) == "2026-09-29T00:00:00-04:00"
    content = entry.find("a:content", NS)
    assert content is not None
    assert content.get("type") == "html"
    assert content.text == (
        "<p>Microsoft reported record revenue and raised guidance.</p>"
        "<ul><li>Microsoft reported record revenue and raised guidance.</li></ul>"
        f'<p><a href="{url}">The filing on sec.gov</a></p>'
        "<p><small>Automated summary; may contain errors and is not investment advice.</small></p>"
    )


@pytest.mark.parametrize(
    ("params", "title", "count"),
    [
        ({"cik": 320193}, "SEC filings: AAPL", 2),
        ({"form_type": "8-K"}, "SEC filings: 8-Ks", 2),
        ({"form_type": "4"}, "SEC filings: Form 4s", 1),
        ({"cik": 320193, "form_type": "8-K"}, "SEC filings: AAPL, 8-Ks", 1),
    ],
)
def test_filters(
    client: TestClient, ids: Any, params: dict[str, Any], title: str, count: int
) -> None:
    feed = parse(client.get("/feeds/filings.atom", params=params))
    assert feed.findtext("a:title", namespaces=NS) == title
    assert len(titles(feed)) == count


def test_one_8k_item(client: TestClient, ids: Any) -> None:
    feed = parse(client.get("/feeds/filings.atom", params={"item": "5.02"}))
    assert feed.findtext("a:title", namespaces=NS) == "SEC filings: 8-K item 5.02"
    assert titles(feed) == ["AAPL 8-K: Apple names Kevan Parekh chief financial officer"]


def test_feed_ids_dont_change_with_new_filters(client: TestClient, ids: Any) -> None:
    # Readers know feeds by id: the ids of feeds without an item stay what they were.
    feed_id = parse(client.get("/feeds/filings.atom", params={"cik": 320193})).findtext(
        "a:id", namespaces=NS
    )
    old = uuid.uuid5(uuid.NAMESPACE_URL, "sec-filings:filings?cik=320193&form_type=")
    assert feed_id == f"urn:uuid:{old}"


def test_unknown_company_or_form(client: TestClient, ids: Any) -> None:
    assert client.get("/feeds/filings.atom", params={"cik": 42}).status_code == 404
    assert client.get("/feeds/filings.atom", params={"form_type": "10-K"}).status_code == 422


def test_the_newest_summary_is_used_and_escaped(
    client: TestClient, seeded: Engine, ids: Any
) -> None:
    with session_factory(seeded).begin() as session:
        session.add(
            Summary(
                accession_no=ids.apple_8k,
                provider="ollama",
                model="llama3.2:3b",
                prompt_version=2,
                summary={
                    "headline": "Apple <b>names</b> a CFO\x07",
                    "summary": "First & foremost.\n\nSecond <script>x</script>.",
                    "key_points": [],
                },
                created_at=datetime(2027, 1, 4, 12, tzinfo=UTC),  # after the seeded ones
            )
        )
    feed = parse(client.get("/feeds/filings.atom", params={"cik": 320193, "form_type": "8-K"}))
    [entry] = feed.findall("a:entry", NS)
    assert entry.findtext("a:title", namespaces=NS) == "AAPL 8-K: Apple <b>names</b> a CFO"
    assert entry.findtext("a:updated", namespaces=NS) == "2027-01-04T12:00:00+00:00"
    content = entry.findtext("a:content", namespaces=NS) or ""
    # Unescaped once by the XML parser, it is HTML with the model's markup as plain text.
    assert content.startswith(
        "<p>First &amp; foremost.</p><p>Second &lt;script&gt;x&lt;/script&gt;.</p><p><a "
    )


def test_the_feed_id_doesnt_depend_on_the_host(client: TestClient, ids: Any) -> None:
    def feed_id(host: str) -> str | None:
        response = client.get("/feeds/filings.atom", headers={"Host": host})
        return parse(response).findtext("a:id", namespaces=NS)

    assert feed_id("api.example.com") == feed_id("localhost")
    assert (feed_id("localhost") or "").startswith("urn:uuid:")


def test_characters_xml_cant_hold_are_dropped() -> None:
    moment = datetime(2026, 10, 22, tzinfo=UTC)
    entry = atom.Entry(
        id="https://example.com/1",
        title="null\x00 and surrogate\ud800 and bell\x07, tab\tkept",
        published=moment,
        updated=moment,
        link="https://example.com/1",
        author="Example",
        category="8-K",
        html="<p>ok</p>",
    )
    body = atom.feed([entry], id="urn:uuid:x", title="T", self_url="https://x/", updated=moment)
    [parsed] = ET.fromstring(body).findall("a:entry", NS)
    assert parsed.findtext("a:title", namespaces=NS) == "null and surrogate and bell, tab\tkept"
