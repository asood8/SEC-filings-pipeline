"""Parsed filing payloads: what the parser stores in MongoDB and later stages read.

Like the events, these models are a contract between services: the parser produces them, and the
summarizer and API consume them. Changing a model's meaning means bumping that form's parser
version.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel

# --- Form 4 (insider trades) ---------------------------------------------------------------------


class Issuer(BaseModel):
    cik: int
    name: str
    trading_symbol: str | None


class ReportingOwner(BaseModel):
    cik: int
    name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str | None
    other_text: str | None


class _Position(BaseModel):
    derivative: bool
    security_title: str
    shares_owned_after: float | None
    direct_or_indirect: Literal["D", "I"] | None
    nature_of_ownership: str | None
    # Derivative securities (options, RSUs, convertibles) only.
    exercise_price: float | None = None
    exercise_date: date | None = None
    expiration_date: date | None = None
    underlying_title: str | None = None
    underlying_shares: float | None = None
    footnote_ids: list[str]


class Transaction(_Position):
    transaction_date: date | None
    # P open-market purchase, S sale, A grant or award, M option exercise, F tax withholding,
    # G gift, C conversion, D disposition to the issuer, J other (see footnotes), ...
    code: str | None
    equity_swap: bool | None
    shares: float | None
    price_per_share: float | None
    acquired_disposed: Literal["A", "D"] | None


class Holding(_Position):
    """A position reported without a transaction (Form 4 tables' holding rows)."""


class Signature(BaseModel):
    name: str
    date: date | None


class Form4(BaseModel):
    document_type: str
    period_of_report: date
    issuer: Issuer
    reporting_owners: list[ReportingOwner]
    # The filer's checkbox for trades made under a Rule 10b5-1 trading plan; None if not answered.
    aff10b5_one: bool | None
    transactions: list[Transaction]
    holdings: list[Holding]
    footnotes: dict[str, str]
    remarks: str | None
    signatures: list[Signature]


# --- 8-K (current reports) -----------------------------------------------------------------------

# The items defined in SEC's Form 8-K instructions (asset-backed-securities items 6.xx omitted).
ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation or an Obligation under an Off-Balance "
    "Sheet Arrangement of a Registrant",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation or an "
    "Obligation under an Off-Balance Sheet Arrangement",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; "
    "Transfer of Listing",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements or a Related Audit Report or "
    "Completed Interim Review",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers; Election of Directors; Appointment of "
    "Certain Officers; Compensatory Arrangements of Certain Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics, or Waiver of a Provision of the Code "
    "of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


class Item(BaseModel):
    number: str
    title: str
    text: str


class Exhibit(BaseModel):
    """An exhibit filed with the 8-K, as text. Only EX-99 exhibits (press releases, investor
    materials) are collected; contracts and other exhibits are not."""

    type: str  # e.g. "EX-99.1"
    description: str | None
    text: str


class Form8K(BaseModel):
    items: list[Item]
    exhibits: list[Exhibit] = []
