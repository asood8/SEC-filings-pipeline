"""Who is signed in: users, sessions, and the session cookie.

Signing in creates a `sessions` row and sets a cookie holding a random token. The table stores
only the token's SHA-256 hash, so a leaked table or backup can't be replayed as sessions. The
cookie is HttpOnly (page scripts can't read it) and SameSite=Lax (browsers don't send it on
cross-site POSTs). Routes that need a signed-in user take `CurrentUser`; tests can replace it
with `app.dependency_overrides[current_user]`.
"""

import hashlib
import secrets
from datetime import timedelta
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from filings_api.google import GoogleIdentity, GoogleIdTokenVerifier
from filings_api.queries import SessionDep
from filings_common.db import Channel, Subscription, User, UserSession

SESSION_COOKIE = "session"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request, session: SessionDep) -> User:
    """The signed-in user. 401 when the session cookie is missing, unknown, or expired."""
    token = request.cookies.get(SESSION_COOKIE)
    user = None
    if token:
        user = session.scalars(
            sa.select(User)
            .join(UserSession, UserSession.user_id == User.id)
            .where(
                UserSession.token_hash == hash_token(token),
                UserSession.expires_at > sa.func.now(),
            )
        ).one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def get_google_verifier(request: Request) -> GoogleIdTokenVerifier | None:
    verifier: GoogleIdTokenVerifier | None = request.app.state.google_verifier
    return verifier


GoogleVerifierDep = Annotated[GoogleIdTokenVerifier | None, Depends(get_google_verifier)]


def upsert_user(session: Session, identity: GoogleIdentity) -> User:
    """Find or create the user by Google account ID, never by email: an address can change, and
    a freed address can be given to someone else. A changed address moves the email alerts."""
    session.execute(
        pg_insert(User)
        .values(google_sub=identity.sub, email=identity.email)
        .on_conflict_do_nothing(index_elements=[User.google_sub])
    )
    user = session.scalars(
        sa.select(User).where(User.google_sub == identity.sub).with_for_update()
    ).one()
    if user.email != identity.email:
        _move_email_alerts(session, user.id, identity.email)
        user.email = identity.email
    return user


def _move_email_alerts(session: Session, user_id: int, email: str) -> None:
    """Email alerts only go to the account's own address, so they follow it. That leaves one
    email subscription per company; extras from older data (to other addresses) are merged into
    the first rather than colliding with it on the unique constraint."""
    subscriptions = session.scalars(
        sa.select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.channel == Channel.EMAIL)
        .order_by(Subscription.id)
        .with_for_update()
    ).all()
    kept: dict[int, Subscription] = {}
    for subscription in subscriptions:
        first = kept.setdefault(subscription.cik, subscription)
        if first is not subscription:
            # Empty form_types means every form type.
            both = first.form_types and subscription.form_types
            first.form_types = sorted({*first.form_types, *subscription.form_types}) if both else []
            session.delete(subscription)
    session.flush()  # delete the extras before retargeting, or they'd collide
    for subscription in kept.values():
        subscription.target = email


def start_session(session: Session, user_id: int, lifetime: timedelta) -> str:
    """Create a session and return its token, which belongs in the cookie and nowhere else.
    Also deletes the user's expired sessions, so they don't pile up."""
    session.execute(
        sa.delete(UserSession).where(
            UserSession.user_id == user_id, UserSession.expires_at <= sa.func.now()
        )
    )
    token = secrets.token_urlsafe(32)
    session.execute(
        sa.insert(UserSession).values(
            user_id=user_id, token_hash=hash_token(token), expires_at=sa.func.now() + lifetime
        )
    )
    return token


def end_session(session: Session, token: str) -> None:
    session.execute(sa.delete(UserSession).where(UserSession.token_hash == hash_token(token)))


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    lifetime: timedelta = request.app.state.session_lifetime
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(lifetime.total_seconds()),
        path="/",
        secure=request.app.state.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=request.app.state.cookie_secure,
        httponly=True,
        samesite="lax",
    )
