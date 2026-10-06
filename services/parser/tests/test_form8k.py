"""8-K parser tests against real EDGAR documents in tests/fixtures/form8k/ (named by accession
number). Expected item lists are SEC's own, from each filing's entry in the submissions API."""

import json
from pathlib import Path

import pytest

from filings_common.parsed import ITEM_TITLES, Form8K
from filings_parser.form8k import Form8KParseError, html_to_lines, parse, parse_form8k

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "form8k"

SEC_ITEMS = {
    "0000002488-26-000182": ["3.02"],
    "0000320187-26-000143": ["5.02", "5.07", "9.01"],
    "0000320187-26-000184": ["2.02", "2.05", "9.01"],
    "0000796343-26-000147": ["2.02", "9.01"],
    "0001018724-26-000036": ["5.02"],
    "0001104659-26-107526": ["8.01", "9.01"],
    "0001628280-26-063820": ["1.01", "1.02", "2.03", "9.01"],
}


def fixture(accession_no: str) -> Form8K:
    return parse_form8k((FIXTURES / f"{accession_no}.htm").read_bytes())


def test_every_fixture_is_covered() -> None:
    assert sorted(p.stem for p in FIXTURES.glob("*.htm")) == sorted(SEC_ITEMS)


@pytest.mark.parametrize("accession_no", sorted(SEC_ITEMS))
def test_items_match_secs_item_list(accession_no: str) -> None:
    form = fixture(accession_no)
    assert [item.number for item in form.items] == SEC_ITEMS[accession_no]
    for item in form.items:
        assert item.title == ITEM_TITLES[item.number]
        assert item.text, f"Item {item.number} has no text"
        assert not any(line.upper().startswith("SIGNATURE") for line in item.text.splitlines())


def test_earnings_release_item() -> None:
    [results, exhibits] = fixture("0000796343-26-000147").items
    assert results.text.startswith("On September 10, 2026, Adobe Inc.")
    assert "attached hereto as Exhibit 99.1" in results.text
    assert "Adobe Reports Record Q3 Results" in exhibits.text


def test_references_inside_sentences_do_not_split_items() -> None:
    items = {i.number: i for i in fixture("0001628280-26-063820").items}
    assert items["2.03"].text == (
        "The information set forth in Item 1.01 above is incorporated herein by reference."
    )
    assert "$20.0 billion senior unsecured" in items["1.01"].text


@pytest.mark.parametrize(
    ("accession_no", "phrase"),
    [
        ("0001018724-26-000036", "elected Kevin R. Mandia as a director"),
        ("0001104659-26-107526", "pursuant to an Underwriting Agreement dated September 9, 2026"),
    ],
)
def test_table_of_contents_is_skipped(accession_no: str, phrase: str) -> None:
    # These filings open with a table of contents (headings, page numbers, "Signatures").
    assert phrase in "\n".join(item.text for item in fixture(accession_no).items)


def test_vote_results_table_rows_stay_on_one_line() -> None:
    votes = next(i for i in fixture("0000320187-26-000143").items if i.number == "5.07")
    assert "For Against Abstain Broker Non-Votes" in votes.text


def test_payload_is_json_ready() -> None:
    payload = parse((FIXTURES / "0000002488-26-000182.htm").read_bytes())
    assert json.loads(json.dumps(payload)) == payload
    assert payload["items"][0]["number"] == "3.02"


def test_html_to_lines_skips_hidden_content_and_breaks_on_blocks() -> None:
    html = (
        "<html><head><title>x</title><style>p{}</style></head><body>"
        '<div style="display: none"><ix:header>hidden facts</ix:header></div>'
        "<div>Item&#160;2.02 Results<br>of Operations</div>"
        "<table><tr><td>For</td><td>1,000</td></tr></table>"
        "<script>ignored()</script><p>  spaced   out </p></body></html>"
    )
    assert html_to_lines(html) == ["Item 2.02 Results", "of Operations", "For 1,000", "spaced out"]


def test_rejects_documents_without_items() -> None:
    with pytest.raises(Form8KParseError, match=r"no 'Item N\.NN' sections"):
        parse_form8k(b"<html><body><p>Nothing to see.</p></body></html>")
