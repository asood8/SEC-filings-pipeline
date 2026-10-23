"""Read a company's recent filings from the EDGAR submissions API response."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class FilingRef:
    accession_no: str
    form_type: str
    filed_at: date
    accepted_at: datetime | None
    primary_document: str


def recent_filings(submissions: dict[str, Any]) -> list[FilingRef]:
    """Filings from `filings.recent`, which the API stores column-wise (one array per field).

    Filings without a primary document (e.g. paper filings) are skipped.
    """
    recent = submissions["filings"]["recent"]
    columns = zip(
        recent["accessionNumber"],
        recent["form"],
        recent["filingDate"],
        recent["acceptanceDateTime"],
        recent["primaryDocument"],
        strict=True,
    )
    return [
        FilingRef(
            accession_no=accession_no,
            form_type=form,
            filed_at=date.fromisoformat(filed),
            accepted_at=datetime.fromisoformat(accepted) if accepted else None,
            primary_document=primary,
        )
        for accession_no, form, filed, accepted, primary in columns
        if primary
    ]


def industry(submissions: dict[str, Any]) -> str | None:
    """SEC's description of the company's SIC code ("Electronic Computers"); None when it has
    none (some funds and trusts)."""
    description = submissions.get("sicDescription")
    if not isinstance(description, str):
        return None
    return description.strip() or None


def raw_document_name(primary_document: str) -> str:
    """The stored document's file name inside the filing folder.

    For ownership forms (3/4/5) the API's primary document is an XSL-rendered view such as
    `xslF345X05/form4.xml`; the raw XML is the same file name at the folder root.
    """
    folder, slash, name = primary_document.partition("/")
    if slash and folder.lower().startswith("xsl"):
        return name
    return primary_document
