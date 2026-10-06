"""Form 4 parser tests against real EDGAR documents in tests/fixtures/form4/ (named by accession
number; each was fetched from https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/)."""

import json
from datetime import date
from pathlib import Path

import pytest

from filings_common.parsed import Form4
from filings_parser.form4 import Form4ParseError, parse, parse_form4

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "form4"


def fixture(accession_no: str) -> Form4:
    return parse_form4((FIXTURES / f"{accession_no}.xml").read_bytes())


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.xml")), ids=lambda p: p.stem)
def test_every_fixture_parses_consistently(path: Path) -> None:
    form = parse_form4(path.read_bytes())
    assert form.document_type == "4"
    assert form.reporting_owners
    assert form.transactions
    assert all(t.code for t in form.transactions)
    for row in [*form.transactions, *form.holdings]:
        assert set(row.footnote_ids) <= set(form.footnotes), "cites a footnote that isn't there"


def test_open_market_sale_under_a_10b5_1_plan() -> None:
    form = fixture("0001936006-26-000026")
    assert (form.issuer.cik, form.issuer.trading_symbol) == (1018724, "AMZN")
    [owner] = form.reporting_owners
    assert owner.is_officer
    assert owner.officer_title == "CEO Worldwide Amazon Stores"
    assert form.aff10b5_one is True
    [sale] = form.transactions
    assert (sale.code, sale.shares, sale.price_per_share, sale.acquired_disposed) == (
        "S",
        1000,
        251.5,
        "D",
    )
    assert (sale.shares_owned_after, sale.direct_or_indirect) == (474681, "D")


def test_joint_filers_buying_another_companys_stock() -> None:
    # Found under Berkshire's CIK, but the issuer is Lennar: Berkshire is the insider here.
    form = fixture("0001193125-26-397059")
    assert (form.issuer.cik, form.issuer.name) == (920760, "LENNAR CORP /NEW/")
    assert [o.name for o in form.reporting_owners] == ["BERKSHIRE HATHAWAY INC", "BUFFETT WARREN E"]
    assert all(o.is_ten_percent_owner for o in form.reporting_owners)
    assert form.aff10b5_one is False
    assert len(form.transactions) == 16
    assert {t.code for t in form.transactions} == {"P"}
    first = form.transactions[0]
    assert (first.transaction_date, first.shares, first.price_per_share) == (
        date(2026, 9, 17),
        267585,
        78.41,
    )
    assert first.direct_or_indirect == "I"
    assert first.footnote_ids == ["F3", "F1"]


def test_option_exercise_then_sale() -> None:
    form = fixture("0001065280-26-000269")
    assert [(t.derivative, t.code) for t in form.transactions] == [
        (False, "M"),
        (False, "S"),
        (True, "M"),
    ]
    option = form.transactions[2]
    assert option.security_title == "Non-Qualified Stock Option (right to buy)"
    assert (option.exercise_price, option.expiration_date) == (20.107, date(2028, 1, 2))
    assert (option.underlying_title, option.underlying_shares) == ("Common Stock", 720)
    assert option.shares_owned_after == 230


def test_price_given_only_in_a_footnote() -> None:
    form = fixture("0001628280-26-063531")
    vesting = form.transactions[0]
    assert vesting.price_per_share is None
    assert "F2" in vesting.footnote_ids
    assert form.footnotes["F2"]


def test_derivative_dates_given_only_in_footnotes_and_fractional_shares() -> None:
    form = fixture("0001104659-26-106432")
    rsu = next(t for t in form.transactions if t.derivative)
    assert (rsu.exercise_date, rsu.expiration_date) == (None, None)
    assert rsu.footnote_ids == ["F4"]
    sale = next(t for t in form.transactions if t.code == "S")
    assert (sale.shares, sale.price_per_share, sale.shares_owned_after) == (
        2605.75,
        360.134,
        25972.25,
    )


def test_gift_held_indirectly() -> None:
    gift = next(t for t in fixture("0001197649-26-000012").transactions if t.code == "G")
    assert (gift.shares, gift.direct_or_indirect) == (438000, "I")
    assert gift.shares_owned_after == 467693547
    assert gift.nature_of_ownership


def test_owner_who_is_director_officer_and_ten_percent_owner() -> None:
    form = fixture("0000950103-26-014647")
    [owner] = form.reporting_owners
    assert (owner.is_director, owner.is_officer, owner.is_ten_percent_owner) == (True, True, True)
    assert owner.officer_title == "COB and CEO"
    assert "C" in {t.code for t in form.transactions}
    assert len(form.holdings) == 10


def test_insider_of_another_issuer_with_two_signatures() -> None:
    form = fixture("0000070858-26-000473")
    assert form.issuer.cik == 1083839
    assert [t.code for t in form.transactions] == ["J"]
    assert form.transactions[0].price_per_share is None
    assert len(form.signatures) == 2
    assert form.signatures[0].date == date(2026, 10, 5)


def test_remarks_and_true_false_booleans() -> None:
    form = fixture("0001193125-26-412669")
    assert form.aff10b5_one is True  # written as <aff10b5One>true</aff10b5One>
    assert form.remarks is not None
    assert form.remarks.startswith("Transaction reported in this Form 4")


def test_payload_is_json_ready() -> None:
    payload = parse((FIXTURES / "0001886367-26-000025.xml").read_bytes())
    assert json.loads(json.dumps(payload)) == payload
    assert payload["period_of_report"] == "2026-09-08"
    assert payload["transactions"][0]["shares"] == 227.113


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"<ownershipDocument>", "not well-formed"),
        (b"<edgarSubmission/>", "expected <ownershipDocument>"),
        (b"<ownershipDocument><documentType>4</documentType></ownershipDocument>", "no reporting"),
    ],
)
def test_rejects_documents_that_are_not_form_4s(raw: bytes, error: str) -> None:
    with pytest.raises(Form4ParseError, match=error):
        parse_form4(raw)


def test_rejects_unrecognized_booleans() -> None:
    raw = (FIXTURES / "0001936006-26-000026.xml").read_bytes()
    broken = raw.replace(b"<aff10b5One>1</aff10b5One>", b"<aff10b5One>maybe</aff10b5One>")
    assert broken != raw
    with pytest.raises(Form4ParseError, match="not a boolean"):
        parse_form4(broken)
