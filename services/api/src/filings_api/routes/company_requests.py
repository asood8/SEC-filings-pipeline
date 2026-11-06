"""Suggestions of companies to add to the watchlist, from signed-in users, and the admin's review.

A user suggests a company by ticker, with an optional note, and sees their suggestions' status.
The admin (users.is_admin, set with `filings-admin grant <email>`, never through the API) sees
them grouped by company, most-asked first, and marks a company added (refused until it's on the
watchlist: a change to config/watchlist.toml, made by hand) or declined. The decision applies to
every suggestion of that company. Responses are per user (`Cache-Control: no-store`).
"""

from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from filings_api.auth import CurrentUser
from filings_api.queries import SessionDep, company_by_ticker
from filings_api.schemas import (
    CompanyRef,
    CompanyRequestCreate,
    CompanyRequestDecision,
    CompanyRequestOut,
    CompanySuggestionOut,
    RequestNoteOut,
    RequestStatusName,
)
from filings_common.db import Company, CompanyRequest, RequestStatus, User
from filings_common.targets import mask_email

router = APIRouter(tags=["company suggestions"])

MAX_OPEN_REQUESTS = 10  # per user, so one account can't flood the list


def _ref(company: Company) -> CompanyRef:
    return CompanyRef(cik=company.cik, ticker=company.ticker, name=company.name)


def _out(request: CompanyRequest, company: Company) -> CompanyRequestOut:
    return CompanyRequestOut(
        id=request.id,
        company=_ref(company),
        note=request.note,
        status=request.status.value,
        created_at=request.created_at,
        decided_at=request.decided_at,
    )


@router.post(
    "/company-requests",
    status_code=201,
    summary="Suggest a company for the watchlist",
    responses={
        409: {"description": "Already followed, already suggested, or too many open."},
        422: {"description": "SEC knows no company by that ticker."},
    },
)
def suggest_company(
    session: SessionDep, user: CurrentUser, body: CompanyRequestCreate
) -> CompanyRequestOut:
    company = company_by_ticker(session, body.ticker)
    if company is None:
        raise HTTPException(
            status_code=422, detail=f"SEC knows no company with ticker {body.ticker}"
        )
    if company.on_watchlist:
        raise HTTPException(status_code=409, detail=f"{company.ticker} is already followed")
    # Locked so two suggestions at once can't both slip under the cap.
    session.execute(sa.select(User.id).where(User.id == user.id).with_for_update())
    mine = session.scalars(sa.select(CompanyRequest).where(CompanyRequest.user_id == user.id)).all()
    if any(r.cik == company.cik for r in mine):
        raise HTTPException(status_code=409, detail=f"you already suggested {company.ticker}")
    if sum(r.status is RequestStatus.OPEN for r in mine) >= MAX_OPEN_REQUESTS:
        raise HTTPException(
            status_code=409,
            detail=f"you have {MAX_OPEN_REQUESTS} open suggestions; wait for a decision first",
        )
    request = CompanyRequest(user_id=user.id, cik=company.cik, note=body.note)
    session.add(request)
    session.commit()
    session.refresh(request)
    return _out(request, company)


@router.get("/company-requests", summary="Your company suggestions")
def my_suggestions(session: SessionDep, user: CurrentUser) -> list[CompanyRequestOut]:
    rows = session.execute(
        sa.select(CompanyRequest, Company)
        .join(Company, Company.cik == CompanyRequest.cik)
        .where(CompanyRequest.user_id == user.id)
        .order_by(CompanyRequest.created_at.desc(), CompanyRequest.id.desc())
    ).all()
    return [_out(request, company) for request, company in rows]


def admin_user(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admins only")
    return user


AdminUser = Annotated[User, Depends(admin_user)]


@router.get("/admin/company-requests", summary="Every company suggestion (admins)")
def review_suggestions(
    session: SessionDep,
    admin: AdminUser,
    status: Annotated[RequestStatusName, Query(description="Which to list.")] = "open",
) -> list[CompanySuggestionOut]:
    """Suggestions grouped by company, the most asked-for first."""
    rows = session.execute(
        sa.select(CompanyRequest, Company, User.email)
        .join(Company, Company.cik == CompanyRequest.cik)
        .join(User, User.id == CompanyRequest.user_id)
        .where(CompanyRequest.status == RequestStatus(status))
        .order_by(CompanyRequest.created_at.desc())
    ).all()
    grouped: dict[int, list[tuple[CompanyRequest, Company, str]]] = {}
    for request, company, email in rows:
        grouped.setdefault(company.cik, []).append((request, company, email))
    suggestions = []
    for group in grouped.values():
        company = group[0][1]
        times: list[datetime] = [request.created_at for request, _, _ in group]
        suggestions.append(
            CompanySuggestionOut(
                company=_ref(company),
                on_watchlist=company.on_watchlist,
                requests=len(group),
                first_requested_at=min(times),
                last_requested_at=max(times),
                status=status,
                notes=[
                    RequestNoteOut(note=request.note, by=mask_email(email), at=request.created_at)
                    for request, _, email in group
                    if request.note
                ],
            )
        )
    return sorted(suggestions, key=lambda s: (-s.requests, -s.last_requested_at.timestamp()))


@router.patch(
    "/admin/company-requests/{ticker}",
    summary="Decide on a suggested company (admins)",
    responses={
        404: {"description": "Nobody suggested it."},
        409: {"description": "Marked added while it isn't on the watchlist."},
    },
)
def decide(
    session: SessionDep, admin: AdminUser, ticker: str, body: CompanyRequestDecision
) -> CompanySuggestionOut:
    """Marks every suggestion of the company added, declined, or open again."""
    company = company_by_ticker(session, ticker)
    requests = (
        session.scalars(sa.select(CompanyRequest).where(CompanyRequest.cik == company.cik)).all()
        if company is not None
        else []
    )
    if company is None or not requests:
        raise HTTPException(status_code=404, detail=f"nobody suggested {ticker}")
    status = RequestStatus(body.status)
    if status is RequestStatus.ADDED and not company.on_watchlist:
        # Users read "added" as followed now, so it waits for the watchlist change.
        raise HTTPException(
            status_code=409,
            detail=f"{company.ticker} isn't on the watchlist yet: add it there first",
        )
    decided_at = None if status is RequestStatus.OPEN else sa.func.now()
    session.execute(
        sa.update(CompanyRequest)
        .where(CompanyRequest.cik == company.cik)
        .values(status=status, decided_at=decided_at)
    )
    session.commit()
    [suggestion] = [
        s for s in review_suggestions(session, admin, body.status) if s.company.cik == company.cik
    ]
    return suggestion


def grant(session: Session, email: str, *, admin: bool = True) -> bool:
    """Make the user with this email an admin (or not). False when no such user signed in yet."""
    found = session.execute(
        sa.update(User)
        .where(sa.func.lower(User.email) == email.strip().lower())
        .values(is_admin=admin)
    )
    session.commit()
    return bool(found.rowcount)  # type: ignore[attr-defined]
