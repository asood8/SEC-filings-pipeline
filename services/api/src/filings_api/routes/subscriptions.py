"""The signed-in user's alert subscriptions: list, create, change, delete.

A subscription alerts one target (a Discord or Slack webhook, or the account's own email address)
about new filings of one watchlist company, optionally only some forms.

- Every query is scoped to the caller: someone else's subscription answers 404, exactly like a
  missing one, so ids reveal nothing.
- Writes lock the user's row first (`SELECT ... FOR UPDATE`), so one user's writes run one at a
  time: concurrent requests can't both slip under the cap or past the duplicate check.
- Webhook URLs contain a secret token, so responses mask them and they are never logged.
- Responses are per user, so they carry `Cache-Control: no-store`.
"""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlalchemy.orm import Session

from filings_api.auth import CurrentUser
from filings_api.queries import SessionDep, company_by_ticker
from filings_api.schemas import CompanyRef, SubscriptionCreate, SubscriptionOut, SubscriptionUpdate
from filings_common.db import Channel, Company, Subscription, User
from filings_common.targets import masked_target, target_is_valid

# Per user. Keeps the list short enough to need no pages, and bounds one account's alert fan-out.
MAX_SUBSCRIPTIONS = 25

NO_STORE = {"Cache-Control": "no-store"}
UNIQUE_VIOLATION = "23505"  # Postgres SQLSTATE

# Ids are Postgres bigints: a larger number would be a database error rather than a clean 422.
SubscriptionId = Annotated[int, Path(ge=1, le=2**63 - 1)]

# What a valid webhook URL looks like, for error messages (the patterns are in filings_common).
WEBHOOK_SHAPES = {
    Channel.DISCORD: "a Discord webhook URL, https://discord.com/api/webhooks/<id>/<token>",
    Channel.SLACK: "a Slack webhook URL, https://hooks.slack.com/services/<team>/<id>/<token>",
}


def _no_store(response: Response) -> None:
    response.headers.update(NO_STORE)


router = APIRouter(tags=["subscriptions"], dependencies=[Depends(_no_store)])


def _error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers=NO_STORE)


def _duplicate(ticker: str) -> HTTPException:
    return _error(409, f"you already have a subscription to {ticker} with this channel and target")


def _subscription_out(subscription: Subscription, company: Company) -> SubscriptionOut:
    return SubscriptionOut(
        id=subscription.id,
        company=CompanyRef(cik=company.cik, ticker=company.ticker, name=company.name),
        form_types=subscription.form_types,
        channel=subscription.channel,
        target=masked_target(subscription.channel, subscription.target),
        created_at=subscription.created_at,
    )


def _own_subscription(
    session: Session, user: User, subscription_id: int
) -> tuple[Subscription, Company]:
    row = session.execute(
        sa.select(Subscription, Company)
        .join(Company, Company.cik == Subscription.cik)
        .where(Subscription.id == subscription_id, Subscription.user_id == user.id)
    ).one_or_none()
    if row is None:
        raise _error(404, "subscription not found")
    subscription, company = row
    return subscription, company


def _lock_user(session: Session, user: User) -> None:
    session.execute(sa.select(User.id).where(User.id == user.id).with_for_update())


def _checked_target(user: User, channel: Channel, target: str | None) -> str:
    """The target to store for `channel`, or 422.

    Email alerts only go to the account's own address, so nobody can point alerts at a stranger's
    inbox: an omitted target means that address, and any other address is refused.
    """
    if channel is Channel.EMAIL:
        if target is not None and target.strip().casefold() != user.email.casefold():
            raise _error(422, "email alerts can only go to your account's email address")
        if not target_is_valid(Channel.EMAIL, user.email):
            raise _error(422, "your account's email address can't receive alerts")
        return user.email
    if target is None:
        raise _error(422, f"target is required: {WEBHOOK_SHAPES[channel]}")
    target = target.strip()
    if not target_is_valid(channel, target):
        raise _error(422, f"target must be {WEBHOOK_SHAPES[channel]}")
    return target


def _check_not_duplicate(
    session: Session,
    user: User,
    company: Company,
    channel: Channel,
    target: str,
    *,
    excluding: int | None = None,
) -> None:
    query = sa.select(Subscription.id).where(
        Subscription.user_id == user.id,
        Subscription.cik == company.cik,
        Subscription.channel == channel,
        Subscription.target == target,
    )
    if excluding is not None:
        query = query.where(Subscription.id != excluding)
    if session.scalars(query.limit(1)).first() is not None:
        raise _duplicate(company.ticker)


def _commit(session: Session, company: Company) -> None:
    """Commit. The unique constraint on (user_id, cik, channel, target) backs up the duplicate
    check, e.g. against rows written outside the API: a violation is a 409 too."""
    try:
        session.commit()
    except sa.exc.IntegrityError as exc:
        session.rollback()
        if getattr(exc.orig, "sqlstate", None) == UNIQUE_VIOLATION:
            raise _duplicate(company.ticker) from None
        raise


@router.get(
    "/subscriptions",
    summary="Your subscriptions",
    description=f"Oldest first. At most {MAX_SUBSCRIPTIONS}, so there are no pages.",
)
def list_subscriptions(session: SessionDep, user: CurrentUser) -> list[SubscriptionOut]:
    rows = session.execute(
        sa.select(Subscription, Company)
        .join(Company, Company.cik == Subscription.cik)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.id)
    ).all()
    return [_subscription_out(subscription, company) for subscription, company in rows]


@router.post(
    "/subscriptions",
    status_code=201,
    summary="Subscribe to a company's filings",
    description=(
        "Alerts are available for watchlist companies. For email, `target` defaults to your "
        f"account's address, the only one allowed. At most {MAX_SUBSCRIPTIONS} per account."
    ),
)
def create_subscription(
    session: SessionDep, user: CurrentUser, body: SubscriptionCreate
) -> SubscriptionOut:
    company = company_by_ticker(session, body.ticker)
    if company is None:
        raise _error(422, f"unknown ticker: {body.ticker}")
    if not company.on_watchlist:
        raise _error(
            422,
            f"{company.ticker} is not on the watchlist; filings (and so alerts) are only "
            "collected for watchlist companies",
        )
    target = _checked_target(user, body.channel, body.target)

    _lock_user(session, user)
    count = session.execute(
        sa.select(sa.func.count()).select_from(Subscription).where(Subscription.user_id == user.id)
    ).scalar_one()
    if count >= MAX_SUBSCRIPTIONS:
        raise _error(
            409, f"you have the maximum of {MAX_SUBSCRIPTIONS} subscriptions; delete one first"
        )
    _check_not_duplicate(session, user, company, body.channel, target)
    subscription = Subscription(
        user_id=user.id,
        cik=company.cik,
        form_types=body.form_types,
        channel=body.channel,
        target=target,
    )
    session.add(subscription)
    _commit(session, company)
    return _subscription_out(subscription, company)


@router.patch(
    "/subscriptions/{subscription_id}",
    summary="Change a subscription",
    description=(
        "Omitted fields keep their values. A new channel needs a new `target`, except email, "
        "which uses your account's address."
    ),
)
def update_subscription(
    session: SessionDep,
    user: CurrentUser,
    subscription_id: SubscriptionId,
    body: SubscriptionUpdate,
) -> SubscriptionOut:
    _lock_user(session, user)
    subscription, company = _own_subscription(session, user, subscription_id)
    channel = body.channel or subscription.channel
    if body.target is None and channel == subscription.channel:
        target = subscription.target  # unchanged
    else:
        target = _checked_target(user, channel, body.target)
    if (channel, target) != (subscription.channel, subscription.target):
        _check_not_duplicate(session, user, company, channel, target, excluding=subscription.id)

    if body.form_types is not None:
        subscription.form_types = list(body.form_types)
    subscription.channel, subscription.target = channel, target
    _commit(session, company)
    return _subscription_out(subscription, company)


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=204,
    summary="Unsubscribe",
    description="Its alert history is deleted with it.",
)
def delete_subscription(
    session: SessionDep, user: CurrentUser, subscription_id: SubscriptionId
) -> None:
    deleted = session.scalars(
        sa.delete(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == user.id)
        .returning(Subscription.id)
    ).first()
    if deleted is None:
        raise _error(404, "subscription not found")
    session.commit()
