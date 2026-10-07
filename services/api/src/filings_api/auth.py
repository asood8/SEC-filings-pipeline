"""The signed-in user, as a FastAPI dependency.

Stub: nobody can sign in yet, so `current_user` always answers 401. It is replaced by the sign-in
implementation (Google OIDC) at merge; the contract, `current_user` and `CurrentUser`, stays the
same, so routes that depend on it don't change.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from filings_api.queries import SessionDep
from filings_common.db import User


def current_user(request: Request, session: SessionDep) -> User:
    """The user making the request, loaded in the request's session; 401 if not signed in.

    Stub, replaced by the sign-in implementation at merge: always 401.
    """
    raise HTTPException(status_code=401, detail="not signed in")


CurrentUser = Annotated[User, Depends(current_user)]
