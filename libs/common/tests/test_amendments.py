"""Form families and the filer's explanation of a Form 4/A (filings_common.amendments). The
real Form 4/As are read in services/parser/tests/test_amendments.py."""

from datetime import date

import pytest

from filings_common.amendments import (
    amendment_reason,
    base_form,
    is_amendment,
    with_amendments,
)
from filings_common.parsed import Form4, Issuer, ReportingOwner


@pytest.mark.parametrize(
    ("form_type", "amendment", "base"),
    [("4", False, "4"), ("4/A", True, "4"), ("8-K", False, "8-K"), ("8-K/A", True, "8-K")],
)
def test_forms_and_their_amendments(form_type: str, amendment: bool, base: str) -> None:
    assert (is_amendment(form_type), base_form(form_type)) == (amendment, base)


def test_choosing_a_form_includes_its_amendments() -> None:
    assert with_amendments(["4"]) == ["4", "4/A"]
    assert with_amendments(["8-K", "4"]) == ["4", "4/A", "8-K", "8-K/A"]
    assert with_amendments(["4/A"]) == ["4", "4/A"]
    assert with_amendments([]) == []


def form(remarks: str | None = None, **footnotes: str) -> Form4:
    return Form4(
        document_type="4/A",
        period_of_report=date(2026, 9, 1),
        date_of_original_submission=date(2026, 9, 3),
        issuer=Issuer(cik=1, name="EXAMPLE CORP", trading_symbol="EX"),
        reporting_owners=[
            ReportingOwner(
                cik=2,
                name="DOE JANE",
                is_director=True,
                is_officer=False,
                is_ten_percent_owner=False,
                is_other=False,
                officer_title=None,
                other_text=None,
            )
        ],
        aff10b5_one=None,
        transactions=[],
        holdings=[],
        footnotes=footnotes,
        remarks=remarks,
        signatures=[],
    )


def test_the_reason_is_the_sentence_that_says_it_amends_and_what_follows() -> None:
    reason = amendment_reason(
        form(
            F1="Granted under the Amended and Restated 2021 Stock Plan.",  # not the reason
            F2="The price is a weighted average. This amendment is being filed to correct the"
            " price. The correct price was $10.25.",
        )
    )
    assert (
        reason
        == "This amendment is being filed to correct the price. The correct price was $10.25."
    )


def test_without_saying_it_amends_a_mistake_in_an_earlier_report_is_the_reason() -> None:
    reason = amendment_reason(
        form(F1="Shares were inadvertently overstated by 2,829 in the original Form 4.")
    )
    assert reason == "Shares were inadvertently overstated by 2,829 in the original Form 4."
    assert amendment_reason(form("Exhibit 24 - Power of Attorney")) is None


def test_a_long_reason_is_cut_to_whole_sentences() -> None:
    sentence = "This amendment corrects " + "the figures " * 20 + "reported."  # 273 characters
    assert amendment_reason(form(f"{sentence} {sentence}")) == sentence  # two would be 547
    # One sentence over 400 characters: cut at a word.
    reason = amendment_reason(form("This amendment corrects " + "the figures " * 40 + "reported."))
    assert reason is not None
    assert len(reason) <= 400
    assert reason.endswith("figures…")
