"""One-click unsubscribe from email alerts, with a signed token instead of sign-in (ADR-0012)."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_common.db import Channel, Subscription, User, session_factory
from filings_common.unsubscribe import token

pytestmark = pytest.mark.integration

SECRET = "u" * 64
ClientFactory = Callable[..., TestClient]


@pytest.fixture
def alerts(seeded: Engine) -> dict[str, int]:
    with session_factory(seeded).begin() as session:
        user = User(email="ada@example.com", google_sub="google-ada")
        session.add(user)
        session.flush()
        email = Subscription(
            user_id=user.id, cik=320193, channel=Channel.EMAIL, target="ada@example.com"
        )
        discord = Subscription(
            user_id=user.id,
            cik=320193,
            channel=Channel.DISCORD,
            target="https://discord.com/api/webhooks/1/x",
        )
        session.add_all([email, discord])
        session.flush()
        return {"email": email.id, "discord": discord.id}


def remaining(engine: Engine) -> set[int]:
    with session_factory(engine)() as session:
        return {s.id for s in session.query(Subscription)}


def test_the_link_describes_the_alert_and_one_click_stops_it(
    make_client: ClientFactory, seeded: Engine, alerts: dict[str, int]
) -> None:
    client = make_client(unsubscribe_secret=SECRET)
    signed = token(alerts["email"], SECRET)
    described = client.get("/unsubscribe", params={"token": signed})
    assert described.status_code == 200
    assert described.headers["Cache-Control"] == "no-store"
    assert described.json() == {
        "company": {"cik": 320193, "ticker": "AAPL", "name": "Apple Inc."},
        "query": None,
        "target": "a***@example.com",
    }
    # A mail app's one-click POST: form-encoded, no cookie, no Origin (RFC 8058).
    response = client.post(
        f"/unsubscribe?token={signed}",
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert response.json() == {"unsubscribed": True}
    assert remaining(seeded) == {alerts["discord"]}
    again = client.post("/unsubscribe", params={"token": signed})  # harmless
    assert again.json() == {"unsubscribed": True}
    assert client.get("/unsubscribe", params={"token": signed}).status_code == 404


@pytest.mark.parametrize("kind", ["forged", "other secret", "webhook alert"])
def test_only_a_signed_link_to_an_email_alert_works(
    make_client: ClientFactory, seeded: Engine, alerts: dict[str, int], kind: str
) -> None:
    client = make_client(unsubscribe_secret=SECRET)
    signed: dict[str, Any] = {
        "forged": f"{alerts['email']}." + "A" * 32,
        "other secret": token(alerts["email"], "v" * 64),
        "webhook alert": token(alerts["discord"], SECRET),
    }
    response = client.post("/unsubscribe", params={"token": signed[kind]})
    if kind == "webhook alert":  # a valid signature, but only email alerts have these links
        assert response.status_code == 200
    else:
        assert response.status_code == 400
    assert remaining(seeded) == {alerts["email"], alerts["discord"]}


def test_another_sites_page_still_cant_post_it(
    make_client: ClientFactory, alerts: dict[str, int]
) -> None:
    client = make_client(unsubscribe_secret=SECRET)
    response = client.post(
        "/unsubscribe",
        params={"token": token(alerts["email"], SECRET)},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_without_a_secret_the_links_are_off(
    make_client: ClientFactory, alerts: dict[str, int]
) -> None:
    client = make_client()
    signed = token(alerts["email"], SECRET)
    assert client.get("/unsubscribe", params={"token": signed}).status_code == 404
    assert client.post("/unsubscribe", params={"token": signed}).status_code == 404
