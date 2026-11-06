"""One-click unsubscribe from email alerts (ADR-0012), with a signed token instead of sign-in.

The notifier puts two links in each email alert: the `List-Unsubscribe` header, which mail apps
POST to (RFC 8058: `List-Unsubscribe=One-Click`, form-encoded, no cookies), and a link in the body
to the website's confirmation page, which reads `GET /unsubscribe` and then POSTs. A GET never
changes anything, so link scanners and prefetching can't unsubscribe anyone.

The token (filings_common.unsubscribe) names one email subscription and is signed with
`UNSUBSCRIBE_SECRET`: it is the credential, so the route needs no session and the CSRF check's
JSON rule doesn't apply to it (middleware.CsrfMiddleware). Without a secret configured, both
routes answer 404.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from filings_api.queries import SessionDep
from filings_api.schemas import CompanyRef
from filings_common.db import Channel, Company, Subscription
from filings_common.targets import mask_email
from filings_common.unsubscribe import subscription_id

router = APIRouter(tags=["alerts"])

Token = Annotated[str, Query(min_length=1, max_length=100, description="From the email's link.")]


class UnsubscribeOut(BaseModel):
    company: CompanyRef | None = Field(description="The company the alert follows, if any.")
    query: str | None = Field(description="A keyword alert's words, if any.")
    target: str = Field(description="The email address, partly masked.")


class UnsubscribedOut(BaseModel):
    unsubscribed: bool = Field(description="Always true: the alert is gone, or already was.")


def _secret(request: Request) -> str:
    secret: str | None = request.app.state.unsubscribe_secret
    if secret is None:
        raise HTTPException(status_code=404, detail="unsubscribe links are off")
    return secret


def _id(request: Request, token: str) -> int:
    found = subscription_id(token, _secret(request))
    if found is None:
        raise HTTPException(status_code=400, detail="not a valid unsubscribe link")
    return found


@router.get(
    "/unsubscribe",
    summary="Which email alert an unsubscribe link stops",
    responses={400: {"description": "Not a valid link."}, 404: {"description": "Already gone."}},
)
def describe(session: SessionDep, request: Request, token: Token) -> UnsubscribeOut:
    subscription = session.get(Subscription, _id(request, token))
    if subscription is None or subscription.channel is not Channel.EMAIL:
        raise HTTPException(status_code=404, detail="already unsubscribed")
    company = session.get(Company, subscription.cik) if subscription.cik is not None else None
    return UnsubscribeOut(
        company=None
        if company is None
        else CompanyRef(cik=company.cik, ticker=company.ticker, name=company.name),
        query=subscription.query,
        target=mask_email(subscription.target),
    )


@router.post(
    "/unsubscribe",
    summary="Stop an email alert, one click",
    description=(
        "RFC 8058 one-click unsubscribe: mail apps POST `List-Unsubscribe=One-Click` here, and the "
        "website's confirmation page POSTs too. Deletes the email alert the token names; "
        "repeating it is harmless."
    ),
    responses={400: {"description": "Not a valid link."}},
)
def unsubscribe(session: SessionDep, request: Request, token: Token) -> UnsubscribedOut:
    subscription = session.get(Subscription, _id(request, token))
    if subscription is not None and subscription.channel is Channel.EMAIL:
        session.delete(subscription)
        session.commit()
    return UnsubscribedOut(unsubscribed=True)
