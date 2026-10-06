"""Hand-written samples in the shape of SEC's APIs (trimmed to the fields the code reads).

These test discovery's own logic. Parsers of filing documents are tested against real EDGAR
documents saved in tests/fixtures/.
"""

from collections.abc import Callable
from typing import Any

import pytest

SubmissionsFactory = Callable[..., dict[str, Any]]


@pytest.fixture
def make_submissions() -> SubmissionsFactory:
    """Build a submissions API response from (accession, form, filing date, primary doc) rows."""

    def build(*filings: tuple[str, str, str, str], cik: int = 320193) -> dict[str, Any]:
        return {
            "cik": str(cik),
            "name": "Example Corp",
            "filings": {
                "recent": {
                    "accessionNumber": [f[0] for f in filings],
                    "form": [f[1] for f in filings],
                    "filingDate": [f[2] for f in filings],
                    "acceptanceDateTime": [f"{f[2]}T16:05:12.000Z" for f in filings],
                    "primaryDocument": [f[3] for f in filings],
                },
                "files": [],
            },
        }

    return build


@pytest.fixture
def company_tickers() -> dict[str, Any]:
    return {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [320193, "Apple Inc.", "AAPL", "Nasdaq"],
            [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
            [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
            [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
            [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
            [1234567, "Unlisted Example Co", "ZZZZ", None],
        ],
    }
