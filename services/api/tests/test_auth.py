import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api.auth import SESSION_COOKIE, current_user
from filings_common.db import (
    Channel,
    Notification,
    Subscription,
    User,
    UserSession,
    session_factory,
)

ClientFactory = Callable[..., TestClient]

pytestmark = pytest.mark.integration


def sign_in(client: TestClient, token: str) -> dict[str, Any]:
    response = client.post("/auth/google", json={"credential": token})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def count(engine: Engine, model: type[Any], *where: Any) -> int:
    with engine.connect() as conn:
        return conn.scalar(sa.select(sa.func.count()).select_from(model).where(*where)) or 0


def test_sign_in_creates_the_account_and_sets_a_session_cookie(
    client: TestClient, google: Any, seeded: Engine
) -> None:
    response = client.post("/auth/google", json={"credential": google.token()})
    assert response.status_code == 200
    user = response.json()
    assert user["email"] == "ada@example.com"
    assert set(user) == {"id", "email", "created_at"}
    assert response.headers["Cache-Control"] == "no-store"

    cookie = response.headers["set-cookie"]
    for attribute in ["HttpOnly", "Max-Age=604800", "Path=/", "SameSite=lax", "Secure"]:
        assert attribute in cookie
    assert client.get("/me").json() == user

    # The database only has the token's hash.
    token = client.cookies[SESSION_COOKIE]
    with session_factory(seeded)() as session:
        stored = session.scalars(sa.select(UserSession)).one()
        assert stored.user_id == user["id"]
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in stored.token_hash
        assert (stored.expires_at - stored.created_at).days == 7


def test_cookie_secure_flag_follows_the_setting(make_client: ClientFactory, google: Any) -> None:
    client = make_client(cookie_secure=False)  # local development over plain http
    response = client.post("/auth/google", json={"credential": google.token()})
    assert "Secure" not in response.headers["set-cookie"]


def test_signing_in_again_reuses_the_account(client: TestClient, google: Any) -> None:
    first = sign_in(client, google.token())
    second = sign_in(client, google.token())
    assert second["id"] == first["id"]


def test_accounts_are_matched_by_google_id_never_by_email(
    make_client: ClientFactory, google: Any
) -> None:
    ada = sign_in(make_client(), google.token())
    # Someone else's Google account that now has the same address (e.g. a reassigned one).
    other = sign_in(make_client(), google.token(sub="100000000000000000002"))
    assert other["id"] != ada["id"]


def test_email_change_moves_email_alerts(client: TestClient, google: Any, seeded: Engine) -> None:
    user = sign_in(client, google.token())
    with session_factory(seeded).begin() as session:
        session.add_all(
            [
                Subscription(
                    user_id=user["id"], cik=320193, channel=Channel.EMAIL, target="ada@example.com"
                ),
                Subscription(
                    user_id=user["id"],
                    cik=320193,
                    channel=Channel.DISCORD,
                    target="https://discord.com/api/webhooks/1/hook",
                ),
            ]
        )

    renamed = sign_in(client, google.token(email="ada@newmail.example"))
    assert renamed["id"] == user["id"]
    assert renamed["email"] == "ada@newmail.example"
    with session_factory(seeded)() as session:
        targets = dict(session.execute(sa.select(Subscription.channel, Subscription.target)).all())
    assert targets == {
        Channel.EMAIL: "ada@newmail.example",
        Channel.DISCORD: "https://discord.com/api/webhooks/1/hook",
    }


def test_email_change_merges_older_email_subscriptions(
    client: TestClient, google: Any, seeded: Engine
) -> None:
    """Older data could hold email subscriptions to several addresses for one company. They all
    move to the new address, so they're merged instead of colliding on the unique constraint."""
    user = sign_in(client, google.token())
    with session_factory(seeded).begin() as session:
        for target, form_types in [
            ("ada@example.com", ["4"]),
            ("ada.work@example.com", ["8-K"]),
            ("ada@newmail.example", ["10-K"]),
        ]:
            session.add(
                Subscription(
                    user_id=user["id"],
                    cik=320193,
                    channel=Channel.EMAIL,
                    target=target,
                    form_types=form_types,
                )
            )
        session.add(
            Subscription(
                user_id=user["id"], cik=789019, channel=Channel.EMAIL, target="ada@example.com"
            )
        )

    sign_in(client, google.token(email="ada@newmail.example"))
    with session_factory(seeded)() as session:
        rows = session.execute(
            sa.select(Subscription.cik, Subscription.target, Subscription.form_types).order_by(
                Subscription.cik
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        (320193, "ada@newmail.example", ["10-K", "4", "8-K"]),
        (789019, "ada@newmail.example", []),
    ]


def test_invalid_token_is_a_generic_401_and_is_not_logged(
    client: TestClient, google: Any, seeded: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    token = google.token(aud="someone-else.apps.googleusercontent.com")
    with caplog.at_level(logging.INFO, logger="filings_api"):
        response = client.post("/auth/google", json={"credential": token})
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid Google credential"}
    assert SESSION_COOKIE not in response.cookies
    assert "Audience" in caplog.text  # the reason is logged...
    assert token not in caplog.text  # ...the token never
    assert count(seeded, User) == 0


@pytest.mark.parametrize("body", [{}, {"credential": ""}, {"credential": "x" * 10_000}])
def test_missing_or_oversized_credential_is_a_422(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/auth/google", json=body).status_code == 422


def test_sign_in_without_a_client_id_is_a_503(make_client: ClientFactory, google: Any) -> None:
    client = make_client(google_verifier=None)
    response = client.post("/auth/google", json={"credential": google.token()})
    assert response.status_code == 503
    assert response.json() == {"detail": "sign-in is not configured"}
    assert client.get("/companies").status_code == 200  # everything else still works


@pytest.mark.parametrize("cookie", [None, "", "made-up-token"])
def test_me_needs_a_valid_session(client: TestClient, cookie: str | None) -> None:
    headers = {"Cookie": f"{SESSION_COOKIE}={cookie}"} if cookie is not None else {}
    response = client.get("/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_expired_session_is_rejected(client: TestClient, google: Any, seeded: Engine) -> None:
    sign_in(client, google.token())
    with seeded.begin() as conn:
        conn.execute(sa.update(UserSession).values(expires_at=sa.func.now()))
    assert client.get("/me").status_code == 401


def test_sign_in_deletes_only_expired_sessions(
    make_client: ClientFactory, google: Any, seeded: Engine
) -> None:
    laptop, phone, other = make_client(), make_client(), make_client()
    user = sign_in(laptop, google.token())
    sign_in(phone, google.token())
    sign_in(other, google.token(sub="100000000000000000002", email="bob@example.com"))
    phone_hash = hashlib.sha256(phone.cookies[SESSION_COOKIE].encode()).hexdigest()
    with seeded.begin() as conn:
        conn.execute(
            sa.update(UserSession)
            .where(UserSession.token_hash != phone_hash)
            .values(expires_at=sa.func.now() - sa.text("interval '1 day'"))
        )

    sign_in(phone, google.token())  # a fresh sign-in on the phone sweeps the user's dead sessions
    assert count(seeded, UserSession, UserSession.user_id == user["id"]) == 1
    assert count(seeded, UserSession, UserSession.user_id != user["id"]) == 1  # not other users'
    assert phone.get("/me").status_code == 200


def test_signing_in_again_replaces_the_browsers_session(
    client: TestClient, google: Any, seeded: Engine
) -> None:
    sign_in(client, google.token())
    old = client.cookies[SESSION_COOKIE]
    sign_in(client, google.token())
    assert client.cookies[SESSION_COOKIE] != old
    assert count(seeded, UserSession) == 1
    assert client.get("/me", headers={"Cookie": f"{SESSION_COOKIE}={old}"}).status_code == 401


def test_logout_ends_the_session_and_is_idempotent(
    client: TestClient, google: Any, seeded: Engine
) -> None:
    sign_in(client, google.token())
    token = client.cookies[SESSION_COOKIE]
    response = client.post("/auth/logout")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert response.headers["Cache-Control"] == "no-store"
    assert count(seeded, UserSession) == 0
    # Even a copy of the old cookie no longer works.
    assert client.get("/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"}).status_code == 401
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/logout", headers={"Cookie": "session=junk"}).status_code == 204


def test_logout_keeps_other_devices_signed_in(make_client: ClientFactory, google: Any) -> None:
    laptop, phone = make_client(), make_client()
    sign_in(laptop, google.token())
    sign_in(phone, google.token())
    laptop.post("/auth/logout")
    assert phone.get("/me").status_code == 200


def test_delete_me_removes_the_account_and_everything_attached(
    make_client: ClientFactory, google: Any, seeded: Engine, ids: Any
) -> None:
    client, other = make_client(), make_client()
    user = sign_in(client, google.token())
    bob = sign_in(other, google.token(sub="100000000000000000002", email="bob@example.com"))
    with session_factory(seeded).begin() as session:
        for owner in [user, bob]:
            subscription = Subscription(
                user_id=owner["id"], cik=320193, channel=Channel.EMAIL, target=owner["email"]
            )
            session.add(subscription)
            session.flush()
            session.add(Notification(subscription_id=subscription.id, accession_no=ids.apple_8k))

    response = client.delete("/me")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/me").status_code == 401
    for model in [User, Subscription, Notification, UserSession]:
        assert count(seeded, model) == 1, model  # only Bob's rows are left
    assert other.get("/me").json()["id"] == bob["id"]


def test_delete_me_needs_a_session(client: TestClient) -> None:
    assert client.delete("/me").status_code == 401


def test_current_user_can_be_overridden(client: TestClient) -> None:
    """Other route tests replace sign-in with a fixed user."""
    fake = User(id=42, email="test@example.com", google_sub="x", created_at=datetime.now(UTC))
    assert isinstance(client.app, FastAPI)
    client.app.dependency_overrides[current_user] = lambda: fake
    assert client.get("/me").json()["id"] == 42
