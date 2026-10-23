from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest

from filings_discovery.submissions import industry, raw_document_name, recent_filings

# Type of the make_submissions fixture (conftest.py).
SubmissionsFactory = Callable[..., dict[str, Any]]


def test_reads_column_wise_recent_filings(make_submissions: SubmissionsFactory) -> None:
    doc = make_submissions(
        ("0000320193-26-000101", "8-K", "2026-09-30", "aapl-20260930.htm"),
        ("0001140361-26-000202", "4", "2026-09-29", "xslF345X05/form4.xml"),
    )
    first, second = recent_filings(doc)
    assert first.accession_no == "0000320193-26-000101"
    assert first.form_type == "8-K"
    assert first.filed_at == date(2026, 9, 30)
    assert first.accepted_at == datetime(2026, 9, 30, 16, 5, 12, tzinfo=UTC)
    assert second.primary_document == "xslF345X05/form4.xml"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Electronic Computers", "Electronic Computers"),
        (" Retail-Variety Stores ", "Retail-Variety Stores"),
        ("", None),
        (None, None),
    ],
)
def test_industry(
    make_submissions: SubmissionsFactory, description: str | None, expected: str | None
) -> None:
    doc = make_submissions()
    doc["sicDescription"] = description  # SEC leaves it empty for some funds and trusts
    assert industry(doc) == expected


def test_skips_filings_without_a_primary_document(make_submissions: SubmissionsFactory) -> None:
    doc = make_submissions(("0000320193-26-000101", "SC 13G", "2026-09-30", ""))
    assert recent_filings(doc) == []


@pytest.mark.parametrize(
    ("primary", "raw"),
    [
        ("xslF345X05/form4.xml", "form4.xml"),
        ("XSLF345X03/doc4.xml", "doc4.xml"),
        ("aapl-20260930.htm", "aapl-20260930.htm"),
        ("exhibits/ex99.htm", "exhibits/ex99.htm"),
    ],
)
def test_raw_document_name_strips_xsl_rendering_folder(primary: str, raw: str) -> None:
    assert raw_document_name(primary) == raw
