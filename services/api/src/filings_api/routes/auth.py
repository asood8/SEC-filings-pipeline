import logging

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response

from filings_api.auth import (
    CurrentUser,
    GoogleVerifierDep,
    clear_session_cookie,
    end_session,
    session_token,
    set_session_cookie,
    start_session,
    upsert_user,
)
from filings_api.google import InvalidIdTokenError, SigningKeysUnavailableError
from filings_api.queries import SessionDep
from filings_api.schemas import GoogleSignIn, UserOut
from filings_common.db import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, created_at=user.created_at)


@router.post("/auth/google", summary="Sign in with a Google ID token")
def sign_in_with_google(
    request: Request,
    response: Response,
    body: GoogleSignIn,
    session: SessionDep,
    verifier: GoogleVerifierDep,
) -> UserOut:
    """Exchange the credential from Google's Sign in with Google button for a session cookie.
    The first sign-in creates the account."""
    if verifier is None:
        raise HTTPException(status_code=503, detail="sign-in is not configured")
    try:
        identity = verifier.verify(body.credential)
    except InvalidIdTokenError as exc:
        logger.info("Google sign-in rejected: %s", exc)
        raise HTTPException(status_code=401, detail="invalid Google credential") from None
    except SigningKeysUnavailableError:
        raise HTTPException(status_code=503, detail="sign-in is temporarily unavailable") from None
    if previous := session_token(request):
        end_session(session, previous)  # a sign-in always gets a fresh session token
    user = upsert_user(session, identity)
    token = start_session(session, user.id, request.app.state.session_lifetime)
    session.commit()
    set_session_cookie(request, response, token)
    logger.info("user %d signed in", user.id)
    return _user_out(user)


@router.post("/auth/logout", status_code=204, summary="Sign out")
def sign_out(request: Request, session: SessionDep) -> Response:
    """End this browser's session. Safe to repeat."""
    if token := session_token(request):
        end_session(session, token)
        session.commit()
    response = Response(status_code=204)
    clear_session_cookie(request, response)
    return response


@router.get("/me", summary="The signed-in user")
def get_me(user: CurrentUser) -> UserOut:
    return _user_out(user)


@router.delete("/me", status_code=204, summary="Delete your account")
def delete_me(request: Request, user: CurrentUser, session: SessionDep) -> Response:
    """Delete the account and everything attached to it: subscriptions, alert history, and
    sessions on every device."""
    session.execute(sa.delete(User).where(User.id == user.id))
    session.commit()
    logger.info("user %d deleted their account", user.id)
    response = Response(status_code=204)
    clear_session_cookie(request, response)
    return response
