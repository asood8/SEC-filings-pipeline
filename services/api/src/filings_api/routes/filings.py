from datetime import date
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Path, Query

from filings_api.pagination import decode_cursor, encode_cursor
from filings_api.queries import (
    CikFilter,
    SessionDep,
    filing_select,
    reporting_owners_out,
    to_filing_out,
)
from filings_api.schemas import (
    ExhibitOut,
    FilingDetail,
    FilingOut,
    Form4Details,
    Form4TransactionOut,
    Page,
    SummaryOut,
)
from filings_common.db import (
    Filing,
    FilingExhibit,
    Form4Filing,
    Form4Transaction,
    Form8KFiling,
    Summary,
)
from filings_common.edgar import archive_url
from filings_common.names import company_name
from filings_common.summaries import FilingSummary

router = APIRouter(tags=["filings"])

ACCESSION_PATTERN = r"^\d{10}-\d{2}-\d{6}$"


@router.get("/filings", summary="Latest filings, newest first")
def list_filings(
    session: SessionDep,
    cik: CikFilter = None,
    form_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> Page[FilingOut]:
    query = filing_select().order_by(Filing.filed_at.desc(), Filing.accession_no.desc())
    if cik is not None:
        query = query.where(Filing.cik == cik)
    if form_type is not None:
        query = query.where(Filing.form_type == form_type)
    if cursor is not None:
        filed_at, accession_no = decode_cursor(cursor, 2)
        try:
            after = date.fromisoformat(str(filed_at))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid cursor") from None
        query = query.where(
            sa.tuple_(Filing.filed_at, Filing.accession_no) < sa.tuple_(after, str(accession_no))
        )
    rows = session.execute(query.limit(limit + 1)).all()
    items = [to_filing_out(*row) for row in rows[:limit]]
    more = len(rows) > limit
    next_cursor = encode_cursor(items[-1].filed_at, items[-1].accession_no) if more else None
    return Page(items=items, next_cursor=next_cursor)


@router.get("/filings/{accession_no}", summary="One filing with its current summary")
def get_filing(
    session: SessionDep, accession_no: Annotated[str, Path(pattern=ACCESSION_PATTERN)]
) -> FilingDetail:
    row = session.execute(filing_select().where(Filing.accession_no == accession_no)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="filing not found")
    filing = row[0]
    summary = session.scalars(
        sa.select(Summary)
        .where(Summary.accession_no == accession_no)
        .order_by(Summary.created_at.desc(), Summary.id.desc())
        .limit(1)
    ).first()
    form4 = session.get(Form4Filing, accession_no)
    transactions = (
        session.scalars(
            sa.select(Form4Transaction)
            .where(Form4Transaction.accession_no == accession_no)
            .order_by(Form4Transaction.line)
        ).all()
        if form4 is not None
        else []
    )
    form8k = session.get(Form8KFiling, accession_no)
    exhibits = session.scalars(
        sa.select(FilingExhibit)
        .where(FilingExhibit.accession_no == accession_no)
        .order_by(FilingExhibit.sequence)
    ).all()
    return FilingDetail(
        **to_filing_out(*row).model_dump(),
        summary=(
            SummaryOut(
                **FilingSummary.model_validate(summary.summary).model_dump(),
                provider=summary.provider,
                model=summary.model,
                prompt_version=summary.prompt_version,
                created_at=summary.created_at,
                ungrounded_numbers=summary.ungrounded_numbers,
            )
            if summary is not None
            else None
        ),
        form4=(
            Form4Details(
                period_of_report=form4.period_of_report,
                issuer_cik=form4.issuer_cik,
                issuer_name=company_name(form4.issuer_name),
                issuer_ticker=form4.issuer_ticker,
                reporting_owners=reporting_owners_out(
                    form4.reporting_owners,
                    form4.reporting_owner_ciks,
                    form4.reporting_owner_relationships,
                ),
                transaction_codes=form4.transaction_codes,
                is_10b5_1=form4.is_10b5_1,
                transactions=[Form4TransactionOut.model_validate(t) for t in transactions],
            )
            if form4 is not None
            else None
        ),
        items=form8k.items if form8k is not None else None,
        exhibits=[
            ExhibitOut(
                type=e.exhibit_type,
                filename=e.filename,
                sec_url=archive_url(filing.cik, accession_no, e.filename),
            )
            for e in exhibits
        ],
    )
