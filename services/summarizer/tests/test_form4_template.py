"""Form 4 template summaries of the real EDGAR fixtures in tests/fixtures/form4/."""

from datetime import date
from pathlib import Path

import pytest

from filings_common.summaries import FilingSummary
from filings_parser.form4 import parse_form4
from filings_summarizer.form4_template import summarize_form4
from filings_summarizer.form8k_prompt import Amended

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "form4"


def summary(accession_no: str) -> FilingSummary:
    return summarize_form4(parse_form4((FIXTURES / f"{accession_no}.xml").read_bytes()))


def test_open_market_sale() -> None:
    s = summary("0001936006-26-000026")
    assert (
        s.headline == "Douglas J Herrington sold 1,000 AMZN shares for $251.5K (avg $251.50/share)"
    )
    assert s.summary == (
        "Douglas J Herrington (CEO Worldwide Amazon Stores) reported 1 transaction in Amazon.com"
        " Inc (AMZN) securities on Oct 1, 2026. Douglas J Herrington sold 1,000 AMZN shares for"
        " $251.5K (avg $251.50/share). The filing says the trades were made under a Rule 10b5-1"
        " trading plan. Afterwards, the reported holding was 474,681 AMZN shares directly."
    )
    assert s.key_points == [
        "Sold 1,000 AMZN shares for $251.5K (avg $251.50/share) on Oct 1, 2026",
        "Made under a Rule 10b5-1 trading plan",
    ]


def test_whitespace_in_filed_text_is_normalized() -> None:
    raw = (FIXTURES / "0001936006-26-000026.xml").read_bytes()
    doubled = raw.replace(b"CEO Worldwide Amazon Stores", b"CEO  Worldwide   Amazon Stores")
    assert doubled != raw
    s = summarize_form4(parse_form4(doubled))
    assert "(CEO Worldwide Amazon Stores)" in s.summary


def test_share_classes_are_reported_separately() -> None:
    # Berkshire bought Lennar Class A and Class B stock; adding them up would be wrong.
    s = summary("0001193125-26-397059")
    assert s.headline.startswith(
        "Berkshire Hathaway Inc and Warren E Buffett bought 2,668,508 shares of Class A Common"
        " Stock (LEN) for $206.7M"
    )
    assert "75,021 shares of Class B Common Stock (LEN)" in s.summary
    assert "Filed jointly by 2 reporting owners" in s.key_points
    assert "Afterwards" not in s.summary  # two share classes: no single holding to report


def test_no_holding_sentence_when_trades_span_several_holding_entities() -> None:
    s = summary("0000950103-26-014647")  # sales through CZI Holdings and Biohub
    assert (
        s.headline
        == "Mark Zuckerberg sold 27,474 META shares for $21.4M (avg $777.44/share) (+2 more)"
    )
    assert "(COB and CEO, Director, 10% owner)" in s.summary
    assert "Afterwards" not in s.summary


def test_roles_read_as_on_the_website() -> None:
    # The same labels as the API and the website (filings_common.names.relationship_label).
    assert summary("0001104659-26-106432").summary.startswith("Vaibhav Taneja (CFO) reported")
    assert summary("0001886367-26-000025").summary.startswith("Latriece Watkins (EVP) reported")


def test_exercises_are_not_valued_at_the_strike_price() -> None:
    s = summary("0001065280-26-000269")
    assert (
        "acquired 720 NFLX shares by exercising or converting derivative securities." in s.summary
    )
    assert (
        "exercised or converted 720 Non-Qualified Stock Option (right to buy) (NFLX)" in s.summary
    )


def test_gift_leads_over_tax_withholding() -> None:
    s = summary("0001197649-26-000012")
    assert s.headline == "Jen Hsun Huang gave 438,000 NVDA shares as a gift (+1 more)"
    assert (
        "had 45,728 NVDA shares withheld to pay taxes or an exercise price worth $9.7M" in s.summary
    )


def test_derivative_award() -> None:
    s = summary("0001284323-26-000002")
    assert s.headline == "Brad D Smith received 4,086 Restricted Stock Unit Award (AMZN)"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.xml")), ids=lambda p: p.stem)
def test_every_fixture_gets_a_clean_summary(path: Path) -> None:
    s = summarize_form4(parse_form4(path.read_bytes()))
    text = " ".join([s.headline, s.summary, *s.key_points])
    assert "None" not in text
    assert "  " not in text
    assert s.key_points


# --- Form 4/As (ADR-0021), from tests/fixtures/form4/amendments/ --------------------------------

AMENDMENTS = FIXTURES / "amendments"


def amended(accession_no: str, amends: Amended) -> FilingSummary:
    form = parse_form4((AMENDMENTS / f"{accession_no}.xml").read_bytes())
    return summarize_form4(form, amends)


def test_an_amendment_says_what_it_amends_and_why() -> None:
    # Alphabet: the shares acquired corrected from 47,574 to 94,715.
    s = amended("0001193125-26-061630", Amended(original_filed_at=date(2026, 1, 15)))
    assert (
        s.headline == "Amended: John Kent Walker received 94,715 GOOGL shares as a grant or award"
    )
    assert s.summary.startswith(
        "This amends the Form 4 filed on Jan 15, 2026. The filer's explanation: “This amendment"
        " is being filed to correct the number of shares acquired, which was inadvertently"
        " reported as 47,574 on the Form 4 filed on January 15, 2026. The actual number of shares"
        " acquired was 94,715.” John Kent Walker"
    )
    assert s.key_points[0] == "Amends the Form 4 filed on Jan 15, 2026"


def test_an_amendment_that_changes_no_trade_says_so() -> None:
    # Nike: filed only to attach a power of attorney.
    s = amended(
        "0000320187-26-000129",
        Amended(original_filed_at=date(2026, 5, 15), changes_nothing=True),
    )
    assert s.headline.startswith("Amended: ")
    assert s.summary.startswith(
        "This amends the Form 4 filed on May 15, 2026. It reports the same trades, unchanged."
        " The filer's explanation: “This Amendment on Form 4/A is being filed solely to attach"
        " the Power of Attorney"
    )
    assert s.key_points[:2] == [
        "Amends the Form 4 filed on May 15, 2026",
        "No reported trade changed",
    ]


def test_an_amendment_without_a_known_original_date() -> None:
    s = amended("0001193125-26-188493", Amended(original_filed_at=None))
    assert s.summary.startswith("This amends an earlier Form 4. The filer's explanation: “This")
    assert s.key_points[0] == "Amends an earlier Form 4"
