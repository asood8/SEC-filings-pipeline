"""Cleaning and splitting earnings-call transcripts (ADR-0022).

The samples are written for these tests in the formats of the real transcripts the rules were
built on (JPMorgan's and PepsiCo's FactSet-style PDFs, Nike's, Bank of America's, J&J's,
Microsoft's): the real ones can't be redistributed, so none is saved as a fixture."""

from datetime import date

import pytest

from filings_common.transcripts import call_date, normalize, split

FACTSET = """\
Example Corp (EXM) Q3 2026 Earnings Call Corrected Transcript 13-Oct-2026
1-877-FACTSET www.callstreet.com Copyright © 2001-2026 FactSet CallStreet, LLC
Jane Roe Chief Financial Officer, Example Corp
Thank you. Revenue was $1.2 billion.
................................................................................
Operator: [Operator Instructions] Our first question comes from John Smith with Big Bank \
Securities. Your line is open.
................................................................................
John Smith Analyst, Big Bank Securities, Inc. Q
How are deposits doing?
................................................................................
Jane Roe Chief Financial Officer, Example Corp A
Deposits grew 3%.
7
Operator: Thank you. Our next question comes from Mary Major with Bank of America. Your line \
is open.
Mary Major Analyst, BofA Securities, Inc. Q
And costs?
Jane Roe Chief Financial Officer, Example Corp A
Flat.
"""


def test_factset_style_transcripts_are_cleaned_and_split() -> None:
    text = normalize(FACTSET)
    assert "....." not in text
    assert "FACTSET" not in text
    assert "\n7\n" not in text  # a page number on its own line
    transcript = split(text)
    assert transcript.remarks.endswith("Revenue was $1.2 billion.")
    assert [(e.analyst, e.firm) for e in transcript.exchanges] == [
        ("John Smith", "Big Bank Securities"),
        ("Mary Major", "Bank of America"),
    ]
    assert transcript.exchanges[0].text.startswith("Operator: [Operator Instructions] Our first")
    assert transcript.exchanges[0].text.endswith("Deposits grew 3%.")
    assert transcript.exchanges[1].text.endswith("Flat.")


@pytest.mark.parametrize(
    ("turn", "analyst", "firm"),
    [
        # Nike
        (
            "[OPERATOR]\nOur first question comes from the line of Ann Lee with UBS Securities.\n"
            "[ANN LEE]\nA question.",
            "Ann Lee",
            "UBS Securities",
        ),
        (
            "[OPERATOR]\nOur next question will come from the line of Bo Chen with Goldman Sachs.",
            "Bo Chen",
            "Goldman Sachs",
        ),
        # Bank of America
        (
            "Operator\nWe'll move next to Manan Gosalia with Morgan Stanley.\nManan Gosalia\nHi.",
            "Manan Gosalia",
            "Morgan Stanley",
        ),
        (
            "Operator\nWe'll move on now to Matt O'Connor with Deutsche Bank.",
            "Matt O'Connor",
            "Deutsche Bank",
        ),
        # J&J: only the name and firm
        (
            "Operator\n(Operator Instructions) Asad Haider, Goldman Sachs.\nAsad Haider\nHi.",
            "Asad Haider",
            "Goldman Sachs",
        ),
        ("Operator\nJoanne Wuensch, Citi.", "Joanne Wuensch", "Citi"),
        # Dell: a firm written with initials
        (
            "Operator: And our next question will come from Joseph Cardoso with J.P. Morgan.",
            "Joseph Cardoso",
            "J.P. Morgan",
        ),
        ("Operator\nAsad Haider, J.P. Morgan.", "Asad Haider", "J.P. Morgan"),
        # Microsoft
        (
            "OPERATOR: The next question will come from the line of Tal Liani with Bank of "
            "America. Please proceed.",
            "Tal Liani",
            "Bank of America",
        ),
        # Alphabet
        (
            "Operator: And our last question comes from Michael Nathanson with MoffettNathanson.",
            "Michael Nathanson",
            "MoffettNathanson",
        ),
    ],
)
def test_every_operators_way_of_introducing_a_question_starts_an_exchange(
    turn: str, analyst: str, firm: str
) -> None:
    transcript = split(normalize(f"Prepared remarks.\n{turn}\nThe answer."))
    assert [(e.analyst, e.firm) for e in transcript.exchanges] == [(analyst, firm)]
    assert transcript.remarks == "Prepared remarks."


def test_a_follow_up_from_the_same_analyst_stays_in_the_exchange() -> None:
    text = normalize(
        "Remarks.\n"
        "OPERATOR: Our first question comes from the line of Adam Wood with Morgan Stanley.\n"
        "ADAM WOOD: Hi.\n"
        "OPERATOR: Adam Wood with Morgan Stanley, your line is open again.\n"
        "ADAM WOOD: Sorry, again.\n"
        "OPERATOR: The next question comes from the line of Karl Keirstead with UBS.\n"
    )
    exchanges = split(text).exchanges
    assert [e.analyst for e in exchanges] == ["Adam Wood", "Karl Keirstead"]
    assert "Sorry, again." in exchanges[0].text


def test_operator_turns_that_name_nobody_start_nothing() -> None:
    text = normalize(
        "Operator: Welcome to the call. I'll hand it to Jim Friedland, Head of Investor "
        "Relations.\nRemarks.\nOperator: Thank you. That concludes our session."
    )
    transcript = split(text)
    assert transcript.exchanges == []
    assert transcript.remarks == text


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (
            "FY27 Q1 Earnings Release Conference Call Transcript\nOctober 1, 2026\n",
            date(2026, 10, 1),
        ),
        ("EVENT DATE/TIME: OCTOBER 13, 2026 / 12:30PM GMT", date(2026, 10, 13)),
        ("Q3 2026 Earnings Call Corrected Transcript 13-Oct-2026", date(2026, 10, 13)),
        ("2026-10-14 BAC 3Q Earnings Call", date(2026, 10, 14)),
        ("Second date first: 2026-07-09, then July 1, 2026", date(2026, 7, 9)),
        ("Microsoft FY27 First Quarter Earnings\nWednesday October 28, 2026", date(2026, 10, 28)),
        ("No date here.", None),
    ],
)
def test_the_call_date_is_the_first_date_in_the_header(header: str, expected: date | None) -> None:
    assert call_date(header) == expected


def test_a_date_past_the_header_is_not_the_call_date() -> None:
    assert call_date("x " * 2_000 + "October 1, 2026") is None
