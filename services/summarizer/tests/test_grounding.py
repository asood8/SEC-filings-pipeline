"""Grounding check. The first cases are the ones found in live Gemini summaries of real 8-Ks."""

import pytest

from filings_summarizer.grounding import figure_value, states_value, ungrounded_numbers

AMAZON_NOTES = (
    "closed the sale of £1,250,000,000 aggregate principal amount of its 5.200% notes due 2029,"
    " £1,000,000,000 aggregate principal amount of its 5.550% notes due 2032"
)
TESLA_FACILITIES = (
    "•$20.0 billion senior unsecured three-year delayed draw term loan facility"
    " •$8.0 billion senior unsecured five-year revolving facility"
    " •$2.0 billion senior unsecured 364-day revolving credit facility"
)


def test_computed_totals_are_flagged() -> None:
    summary = "Amazon sold notes totaling £4.25 billion, including £1.25 billion of 2029 notes."
    assert ungrounded_numbers(summary, AMAZON_NOTES) == ["4.25 billion"]
    assert ungrounded_numbers("Tesla Enters $30.0 Billion in Facilities", TESLA_FACILITIES) == [
        "30.0 Billion"
    ]


def test_each_flagged_figure_is_listed_once() -> None:
    summary = "Notes Totaling £4.25 Billion: the £4.25 billion offering"
    assert ungrounded_numbers(summary, AMAZON_NOTES) == ["4.25 Billion"]
    several = "£4.25 Billion, then £9 billion, then £4.25 billion again"
    assert ungrounded_numbers(several, AMAZON_NOTES) == ["4.25 Billion", "9 billion"]


def test_stated_figures_in_another_format_are_grounded() -> None:
    summary = "£1.25 billion of 5.2% notes due 2029 and £1.0 billion due 2032; $20.0 billion loan"
    assert ungrounded_numbers(summary, AMAZON_NOTES + " " + TESLA_FACILITIES) == []


@pytest.mark.parametrize(
    ("summary", "source"),
    [
        ("revenue of $1.3 billion", "revenue of $1,324 million"),  # rounded from its precision
        ("up 12%", "up 11.6%"),  # 11.6 rounds to 12 at the precision the summary states
        ("$405 million deposit", "deposit of $405,000,000"),
        ("over 486,000 vehicles", "delivered over 486,000 vehicles"),
        ("elected 3 new directors", "elected three new directors"),
        ("free cash flow of $14.4 billion", "(in millions) Free Cash Flow $ 5,283 $ 14,402"),
        ("filed on September 17, 2026", "Form 8-K filed 2026-09-17"),
        # Ranges: the unit after the range covers both ends (found with granite4 on Adobe's 8-K).
        ("guidance of $6.80 to $6.85 billion", "revenue $6.80 billion to $6.85 billion"),
        (
            "$1.93-$1.95 billion and $4.665\u20134.695 billion",
            "$1.93 billion to $1.95 billion; $4.665 billion to $4.695 billion",
        ),
        ("between 5 and 10 million users", "5 million to 10 million users"),
        ("Q3 FY27 results under a 10b5-1 plan", "third quarter of fiscal 2027, Rule 10b5-1"),
    ],
)
def test_grounded(summary: str, source: str) -> None:
    assert ungrounded_numbers(summary, source) == []


@pytest.mark.parametrize(
    ("summary", "source", "flagged"),
    [
        ("revenue of $1.4 billion", "revenue of $1,324 million", ["1.4 billion"]),
        ("up 13%", "up 11.6%", ["13"]),
        ("4 directors", "three directors", ["4"]),
        ("$6.80 to $6.95 billion", "revenue $6.80 billion to $6.85 billion", ["6.95 billion"]),
        # "and" without "between" is not a range: 2029 must not become "2029 billion".
        ("notes due 2029 and £9.0 billion", "notes due 2029", ["9.0 billion"]),
        # Only bare table numbers are scaled by "in millions", not ones that state a unit.
        ("$5 quadrillion", "(in millions) a $5 billion facility", ["5"]),
        ("a $5,000 trillion", "(in millions) a $5 billion facility", ["5,000 trillion"]),
    ],
)
def test_flagged(summary: str, source: str, flagged: list[str]) -> None:
    assert ungrounded_numbers(summary, source) == flagged


def test_figure_value_reads_one_written_figure() -> None:
    assert figure_value("£4.242 billion") == pytest.approx(4.242e9)
    assert figure_value("16,000,000") == 16e6
    assert figure_value("5.6%") == 5.6
    for bad in ("no number", "3 to 4 billion"):
        with pytest.raises(ValueError, match="exactly one number"):
            figure_value(bad)


@pytest.mark.parametrize(
    ("text", "value", "stated"),
    [
        ("adding 16 million shares", 16e6, True),
        ("a $1 billion charge", 1.0e9, True),  # rounds from its own precision
        ("revenue of $1.3 billion", 1.324e9, True),
        ("revenue of $1.30 billion", 1.324e9, False),
        ("twelve directors", 12, True),
        ("a loss of $3 to $4 billion", 3e9, True),  # the range's unit covers both ends
        ("over 486,000 vehicles", 486_532, False),
        ("in Q3", 3, False),  # glued to letters: not a figure
    ],
)
def test_states_value(text: str, value: float, stated: bool) -> None:
    assert states_value(text, value) is stated
