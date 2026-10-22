import pytest
from pydantic import ValidationError

from filings_common.summaries import FilingSummary


def test_headline_and_key_points_are_one_line() -> None:
    summary = FilingSummary(
        headline="Apple names\na new CFO\r\n",
        summary="First paragraph.\n\nSecond paragraph.",
        key_points=["Effective\tOctober 1", "  Succeeds  Luca Maestri "],
    )
    assert summary.headline == "Apple names a new CFO"
    assert summary.key_points == ["Effective October 1", "Succeeds Luca Maestri"]
    assert summary.summary == "First paragraph.\n\nSecond paragraph."  # paragraphs stay


def test_a_blank_headline_is_refused() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        FilingSummary(headline=" \n ", summary="S")
