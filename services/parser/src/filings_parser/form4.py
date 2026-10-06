"""Form 4 parser: statements of changes in beneficial ownership (insider trades).

A Form 4 is an XML `ownershipDocument` (SEC's EDGAR Ownership XML Technical Specification). Most
fields hold their data in a `<value>` child, and any field may instead, or as well, cite footnotes
with `<footnoteId id="F1"/>`. A price can be given only in a footnote, for example. So every value
is optional, and each row keeps the IDs of the footnotes it cites.

Parsing uses the standard library: Python's bundled expat rejects entity-expansion attacks, and
ElementTree never fetches external entities.
"""

import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from filings_common.db import Form4Filing
from filings_common.parsed import (
    Form4,
    Holding,
    Issuer,
    ReportingOwner,
    Signature,
    Transaction,
)

PARSER_VERSION = 1


class Form4ParseError(ValueError):
    pass


def _text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    found = element.find(path)
    if found is None:
        return None
    text = "".join(found.itertext()).strip()
    return text or None


def _value(element: ET.Element, path: str) -> str | None:
    return _text(element, f"{path}/value")


def _number(element: ET.Element, path: str) -> float | None:
    text = _value(element, path)
    return float(text.replace(",", "")) if text else None


def _date(element: ET.Element, path: str) -> date | None:
    text = _value(element, path)
    # xs:date may carry a timezone suffix (2026-09-10-05:00); the calendar date is what matters.
    return date.fromisoformat(text[:10]) if text else None


def _flag(element: ET.Element | None, path: str) -> bool | None:
    text = _text(element, path)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in ("1", "true"):
        return True
    if lowered in ("0", "false"):
        return False
    raise Form4ParseError(f"not a boolean in <{path}>: {text!r}")


def _footnote_ids(element: ET.Element) -> list[str]:
    ids = (f.get("id") for f in element.iter("footnoteId"))
    return list(dict.fromkeys(i for i in ids if i))


def _required(value: str | None, what: str) -> str:
    if value is None:
        raise Form4ParseError(f"missing {what}")
    return value


def _position_fields(row: ET.Element, *, derivative: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "derivative": derivative,
        "security_title": _required(_value(row, "securityTitle"), "securityTitle"),
        "shares_owned_after": _number(
            row, "postTransactionAmounts/sharesOwnedFollowingTransaction"
        ),
        "direct_or_indirect": _value(row, "ownershipNature/directOrIndirectOwnership"),
        "nature_of_ownership": _value(row, "ownershipNature/natureOfOwnership"),
        "footnote_ids": _footnote_ids(row),
    }
    if derivative:
        fields |= {
            "exercise_price": _number(row, "conversionOrExercisePrice"),
            "exercise_date": _date(row, "exerciseDate"),
            "expiration_date": _date(row, "expirationDate"),
            "underlying_title": _value(row, "underlyingSecurity/underlyingSecurityTitle"),
            "underlying_shares": _number(row, "underlyingSecurity/underlyingSecurityShares"),
        }
    return fields


def _transaction(row: ET.Element, *, derivative: bool) -> Transaction:
    return Transaction(
        **_position_fields(row, derivative=derivative),
        transaction_date=_date(row, "transactionDate"),
        code=_text(row, "transactionCoding/transactionCode"),
        equity_swap=_flag(row, "transactionCoding/equitySwapInvolved"),
        shares=_number(row, "transactionAmounts/transactionShares"),
        price_per_share=_number(row, "transactionAmounts/transactionPricePerShare"),
        acquired_disposed=_value(row, "transactionAmounts/transactionAcquiredDisposedCode"),
    )


def _owner(element: ET.Element) -> ReportingOwner:
    relationship = element.find("reportingOwnerRelationship")
    return ReportingOwner(
        cik=int(_required(_text(element, "reportingOwnerId/rptOwnerCik"), "rptOwnerCik")),
        name=_required(_text(element, "reportingOwnerId/rptOwnerName"), "rptOwnerName"),
        is_director=_flag(relationship, "isDirector") or False,
        is_officer=_flag(relationship, "isOfficer") or False,
        is_ten_percent_owner=_flag(relationship, "isTenPercentOwner") or False,
        is_other=_flag(relationship, "isOther") or False,
        officer_title=_text(relationship, "officerTitle"),
        other_text=_text(relationship, "otherText"),
    )


def parse_form4(raw: bytes) -> Form4:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise Form4ParseError(f"not well-formed XML: {exc}") from exc
    if root.tag != "ownershipDocument":
        raise Form4ParseError(f"expected <ownershipDocument>, got <{root.tag}>")

    owners = [_owner(o) for o in root.findall("reportingOwner")]
    if not owners:
        raise Form4ParseError("no reportingOwner")
    period = _required(_text(root, "periodOfReport"), "periodOfReport")
    signatures = [
        Signature(
            name=_required(_text(s, "signatureName"), "signatureName"),
            date=date.fromisoformat(d[:10]) if (d := _text(s, "signatureDate")) else None,
        )
        for s in root.findall("ownerSignature")
    ]
    return Form4(
        document_type=_required(_text(root, "documentType"), "documentType"),
        period_of_report=date.fromisoformat(period[:10]),
        issuer=Issuer(
            cik=int(_required(_text(root, "issuer/issuerCik"), "issuerCik")),
            name=_required(_text(root, "issuer/issuerName"), "issuerName"),
            trading_symbol=_text(root, "issuer/issuerTradingSymbol"),
        ),
        reporting_owners=owners,
        aff10b5_one=_flag(root, "aff10b5One"),
        transactions=[
            *(
                _transaction(t, derivative=False)
                for t in root.findall("nonDerivativeTable/nonDerivativeTransaction")
            ),
            *(
                _transaction(t, derivative=True)
                for t in root.findall("derivativeTable/derivativeTransaction")
            ),
        ],
        holdings=[
            *(
                Holding(**_position_fields(h, derivative=False))
                for h in root.findall("nonDerivativeTable/nonDerivativeHolding")
            ),
            *(
                Holding(**_position_fields(h, derivative=True))
                for h in root.findall("derivativeTable/derivativeHolding")
            ),
        ],
        footnotes={
            f.get("id", ""): "".join(f.itertext()).strip()
            for f in root.findall("footnotes/footnote")
        },
        remarks=_text(root, "remarks"),
        signatures=signatures,
    )


def parse(raw: bytes) -> dict[str, Any]:
    """The JSON-ready payload stored in MongoDB."""
    return parse_form4(raw).model_dump(mode="json")


def save_key_fields(session: Session, accession_no: str, payload: dict[str, Any]) -> None:
    """Upsert the queryable summary of a Form 4 into Postgres (`form4_filings`)."""
    form = Form4.model_validate(payload)
    values = {
        "period_of_report": form.period_of_report,
        "issuer_cik": form.issuer.cik,
        "issuer_name": form.issuer.name,
        "issuer_ticker": form.issuer.trading_symbol,
        "reporting_owners": [o.name for o in form.reporting_owners],
        "transaction_codes": sorted({t.code for t in form.transactions if t.code}),
        "is_10b5_1": form.aff10b5_one,
    }
    stmt = pg_insert(Form4Filing).values(accession_no=accession_no, **values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Form4Filing.accession_no],
            set_={**values, "updated_at": sa.func.now()},
        )
    )
