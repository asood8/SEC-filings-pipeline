"""Subscriptions CRUD. Users are signed in by overriding the `current_user` dependency."""

import logging
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import httpx2
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api.auth import current_user
from filings_api.queries import SessionDep
from filings_api.routes import subscriptions
from filings_api.routes.subscriptions import MAX_SUBSCRIPTIONS
from filings_common.db import Company, Notification, Subscription, User, session_factory

pytestmark = pytest.mark.integration

ClientFactory = Callable[..., TestClient]
SignIn = Callable[[str], TestClient]

TOKEN = "s3cr3t-Token_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTU1234"
DISCORD = f"https://discord.com/api/webhooks/112233445566778899/{TOKEN}"
DISCORD_MASKED = "https://discord.com/api/webhooks/112233445566778899/…1234"
SLACK = "https://hooks.slack.com/services/T0AB12CD3/B0EF45GH6/aBcDeFgHiJkLmNoPqRsT9876"
SLACK_MASKED = "https://hooks.slack.com/services/T0AB12CD3/B0EF45GH6/…9876"
ALICE_EMAIL = "Alice@example.com"


@pytest.fixture
def users(seeded: Engine) -> dict[str, int]:
    """Two users, and Berkshire on the watchlist (a ticker SEC writes with a dash)."""
    with session_factory(seeded).begin() as session:
        session.add(
            Company(cik=1067983, ticker="BRK-B", name="BERKSHIRE HATHAWAY INC", on_watchlist=True)
        )
        alice = User(email=ALICE_EMAIL, google_sub="google-alice")
        bob = User(email="bob@example.com", google_sub="google-bob")
        session.add_all([alice, bob])
        session.flush()
        return {"alice": alice.id, "bob": bob.id}


@pytest.fixture
def sign_in(make_client: ClientFactory, users: dict[str, int]) -> SignIn:
    """A client signed in as the named user."""

    def build(name: str) -> TestClient:
        client = make_client()
        user_id = users[name]

        def signed_in(session: SessionDep) -> User:
            return session.get_one(User, user_id)

        cast(FastAPI, client.app).dependency_overrides[current_user] = signed_in
        return client

    return build


def subscribe(client: TestClient, **fields: Any) -> httpx2.Response:
    body = {"ticker": "AAPL", "channel": "discord", "target": DISCORD} | fields
    return client.post("/subscriptions", json=body)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/subscriptions", None),
        ("POST", "/subscriptions", {"ticker": "AAPL", "channel": "email"}),
        ("PATCH", "/subscriptions/1", {"form_types": ["4"]}),
        ("DELETE", "/subscriptions/1", None),
    ],
)
def test_requires_sign_in(
    make_client: ClientFactory, method: str, path: str, body: dict[str, Any] | None
) -> None:
    assert make_client().request(method, path, json=body).status_code == 401


def test_create_list_change_and_delete(sign_in: SignIn) -> None:
    alice = sign_in("alice")
    created = subscribe(alice, form_types=["8-K", "4", "8-K"])
    assert created.status_code == 201
    subscription = created.json()
    assert subscription["company"] == {"cik": 320193, "ticker": "AAPL", "name": "Apple Inc."}
    assert subscription["form_types"] == ["4", "8-K"]  # deduplicated and sorted
    assert (subscription["channel"], subscription["target"]) == ("discord", DISCORD_MASKED)
    assert alice.get("/subscriptions").json() == [subscription]

    path = f"/subscriptions/{subscription['id']}"
    changes: list[tuple[dict[str, Any], dict[str, Any]]] = [
        ({"form_types": ["8-K"]}, {"form_types": ["8-K"]}),
        ({"channel": "slack", "target": SLACK}, {"channel": "slack", "target": SLACK_MASKED}),
        ({"channel": "email"}, {"channel": "email", "target": ALICE_EMAIL}),  # own address
        ({"form_types": []}, {"form_types": []}),  # every form
        ({}, {}),
    ]
    for change, expected in changes:
        response = alice.patch(path, json=change)
        assert response.status_code == 200, change
        subscription |= expected
        assert response.json() == subscription
    assert alice.get("/subscriptions").json() == [subscription]

    deleted = alice.delete(path)
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert alice.get("/subscriptions").json() == []
    assert alice.delete(path).status_code == 404


def test_responses_are_never_cached(sign_in: SignIn) -> None:
    alice = sign_in("alice")
    created = subscribe(alice)
    path = f"/subscriptions/{created.json()['id']}"
    responses = [
        created,
        alice.get("/subscriptions"),
        alice.patch(path, json={"form_types": ["4"]}),
        alice.delete(path),
        alice.delete(path),  # 404
        subscribe(alice, ticker="NOPE"),  # 422
    ]
    assert [r.status_code for r in responses] == [201, 200, 200, 204, 404, 422]
    assert all(r.headers["Cache-Control"] == "no-store" for r in responses)


def test_tickers_are_normalized(sign_in: SignIn) -> None:
    response = subscribe(sign_in("alice"), ticker=" brk.b ")
    assert response.status_code == 201
    assert response.json()["company"]["ticker"] == "BRK-B"


def test_webhook_urls_are_stored_in_full_but_shown_masked(
    sign_in: SignIn, seeded: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    alice = sign_in("alice")
    created = subscribe(alice)
    slack = subscribe(alice, channel="slack", target=SLACK)
    listed = alice.get("/subscriptions")

    assert [s["target"] for s in listed.json()] == [DISCORD_MASKED, SLACK_MASKED]
    for response in (created, slack, listed):
        assert TOKEN not in response.text
        assert SLACK not in response.text
    assert TOKEN not in caplog.text
    with session_factory(seeded)() as session:
        assert set(session.scalars(sa.select(Subscription.target))) == {DISCORD, SLACK}


def test_email_alerts_go_only_to_the_account_address(sign_in: SignIn) -> None:
    alice = sign_in("alice")
    default = subscribe(alice, channel="email", target=None)
    assert default.status_code == 201
    assert default.json()["target"] == ALICE_EMAIL  # shown in full
    any_case = subscribe(alice, ticker="MSFT", channel="email", target=" alice@EXAMPLE.com ")
    assert any_case.status_code == 201
    assert any_case.json()["target"] == ALICE_EMAIL

    stranger = subscribe(alice, ticker="BRK-B", channel="email", target="bob@example.com")
    assert stranger.status_code == 422
    assert "your account's email address" in stranger.json()["detail"]
    injected = f"{ALICE_EMAIL}\nBcc: victim@example.com"
    assert subscribe(alice, ticker="BRK-B", channel="email", target=injected).status_code == 422

    # Changing a subscription follows the same rule.
    email_path = f"/subscriptions/{default.json()['id']}"
    assert alice.patch(email_path, json={"target": "bob@example.com"}).status_code == 422
    webhook = subscribe(alice, ticker="BRK-B").json()
    switch = alice.patch(
        f"/subscriptions/{webhook['id']}", json={"channel": "email", "target": "bob@example.com"}
    )
    assert switch.status_code == 422
    # Moving to a webhook channel needs a webhook URL.
    to_slack = alice.patch(email_path, json={"channel": "slack"})
    assert to_slack.status_code == 422
    assert "target is required" in to_slack.json()["detail"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ticker": "NOPE"}, "unknown ticker: NOPE"),
        ({"ticker": "ZZZZ"}, "ZZZZ is not on the watchlist"),
        ({"form_types": ["4", "10-K"]}, "Input should be '4' or '8-K'"),
        ({"channel": "sms"}, "Input should be 'discord', 'slack' or 'email'"),
        ({"target": None}, "target is required: a Discord webhook URL, https://discord.com/"),
        ({"cik": 320193}, "Extra inputs are not permitted"),  # refused, not silently ignored
        ({"ticker": ""}, "at least 1 character"),
    ],
)
def test_invalid_subscriptions_are_refused(
    sign_in: SignIn, fields: dict[str, Any], message: str
) -> None:
    response = subscribe(sign_in("alice"), **fields)
    assert response.status_code == 422
    assert message in response.text


@pytest.mark.parametrize(
    ("channel", "target"),
    [
        ("discord", "http://discord.com/api/webhooks/1/abc"),  # not https
        ("discord", "https://169.254.169.254/latest/meta-data/"),  # cloud metadata endpoint
        ("discord", "https://127.0.0.1/api/webhooks/1/abc"),
        ("discord", "https://[::1]/api/webhooks/1/abc"),
        ("discord", "https://10.0.0.5:8080/api/webhooks/1/abc"),
        ("discord", "https://localhost/api/webhooks/1/abc"),
        ("discord", "https://discord.com.evil.example/api/webhooks/1/abc"),  # look-alike hosts
        ("discord", "https://evildiscord.com/api/webhooks/1/abc"),
        ("discord", "https://discord.com@10.0.0.1/api/webhooks/1/abc"),  # userinfo trick
        ("discord", "https://discord.com:8443/api/webhooks/1/abc"),
        ("discord", "https://discord.com/api/webhooks/1/abc/../../../admin"),
        ("discord", "https://discord.com/api/webhooks/1/abc?next=http://10.0.0.1/"),
        ("discord", SLACK),  # the other service
        ("slack", "https://hooks.slack.com.evil.example/services/T0/B0/abc"),
        ("slack", "https://hooks.slack.com/services/T0/B0/abc#@10.0.0.1"),
        ("slack", "file:///etc/passwd"),
        ("slack", DISCORD),
    ],
)
def test_webhook_targets_must_be_official_webhook_urls(
    sign_in: SignIn, channel: str, target: str
) -> None:
    response = subscribe(sign_in("alice"), channel=channel, target=target)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("target must be a ")
    assert "webhook URL, https://" in response.json()["detail"]


def test_users_only_see_and_change_their_own(sign_in: SignIn) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    mine = subscribe(alice).json()
    path = f"/subscriptions/{mine['id']}"

    assert bob.get("/subscriptions").json() == []
    change = {"form_types": ["4"]}
    foreign, missing = bob.patch(path, json=change), bob.patch("/subscriptions/999999", json=change)
    assert (foreign.status_code, foreign.json()) == (404, {"detail": "subscription not found"})
    assert (missing.status_code, missing.json()) == (foreign.status_code, foreign.json())
    foreign, missing = bob.delete(path), bob.delete("/subscriptions/999999")
    assert (foreign.status_code, foreign.json()) == (404, {"detail": "subscription not found"})
    assert (missing.status_code, missing.json()) == (foreign.status_code, foreign.json())

    assert alice.get("/subscriptions").json() == [mine]  # untouched
    assert subscribe(bob).status_code == 201  # the same target is fine for another user


@pytest.mark.parametrize("subscription_id", [0, 2**63])  # not a Postgres bigint id
def test_out_of_range_ids_are_refused_cleanly(sign_in: SignIn, subscription_id: int) -> None:
    alice = sign_in("alice")
    assert alice.patch(f"/subscriptions/{subscription_id}", json={}).status_code == 422
    assert alice.delete(f"/subscriptions/{subscription_id}").status_code == 422


def test_subscriptions_are_capped_per_user(sign_in: SignIn) -> None:
    alice = sign_in("alice")
    for i in range(MAX_SUBSCRIPTIONS):
        target = f"https://discord.com/api/webhooks/{i}/{TOKEN}"
        assert subscribe(alice, target=target).status_code == 201
    over = subscribe(alice)
    assert over.status_code == 409
    assert f"maximum of {MAX_SUBSCRIPTIONS} subscriptions" in over.json()["detail"]
    assert len(alice.get("/subscriptions").json()) == MAX_SUBSCRIPTIONS
    assert subscribe(sign_in("bob")).status_code == 201  # per user

    first = alice.get("/subscriptions").json()[0]
    assert alice.delete(f"/subscriptions/{first['id']}").status_code == 204
    assert subscribe(alice).status_code == 201  # a slot freed up


def test_concurrent_creates_cannot_exceed_the_cap(sign_in: SignIn) -> None:
    alice = sign_in("alice")
    for i in range(MAX_SUBSCRIPTIONS - 2):
        target = f"https://discord.com/api/webhooks/{i}/{TOKEN}"
        assert subscribe(alice, target=target).status_code == 201
    clients = [sign_in("alice") for _ in range(8)]  # one per thread

    def create(n: int) -> int:
        target = f"https://discord.com/api/webhooks/{100 + n}/{TOKEN}"
        return subscribe(clients[n], target=target).status_code

    with ThreadPoolExecutor(len(clients)) as pool:
        statuses = sorted(pool.map(create, range(len(clients))))
    assert statuses == [201, 201] + [409] * 6
    assert len(alice.get("/subscriptions").json()) == MAX_SUBSCRIPTIONS


def test_duplicates_are_refused(sign_in: SignIn) -> None:
    alice = sign_in("alice")
    first = subscribe(alice, form_types=["4"]).json()
    again = subscribe(alice, form_types=["8-K"])  # other forms, but the same alert target
    assert again.status_code == 409
    assert "already have a subscription to AAPL" in again.json()["detail"]
    assert subscribe(alice, ticker="MSFT").status_code == 201  # another company is fine
    assert subscribe(alice, channel="email", target=None).status_code == 201
    assert subscribe(alice, channel="email", target=ALICE_EMAIL.upper()).status_code == 409

    # Changing a subscription into a copy of another is refused too.
    slack = subscribe(alice, channel="slack", target=SLACK).json()
    clash = alice.patch(
        f"/subscriptions/{slack['id']}", json={"channel": "discord", "target": DISCORD}
    )
    assert clash.status_code == 409
    # Restating its own target is not a clash.
    same = alice.patch(f"/subscriptions/{first['id']}", json={"target": DISCORD})
    assert same.status_code == 200


@pytest.fixture
def unique_targets(seeded: Engine) -> Iterator[None]:
    """A unique index like the constraint the sign-in migration adds on (user_id, cik, channel,
    target), dropped afterwards."""
    with seeded.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE UNIQUE INDEX test_subscriptions_unique "
                "ON subscriptions (user_id, cik, channel, target)"
            )
        )
    yield
    with seeded.begin() as conn:
        conn.execute(sa.text("DROP INDEX test_subscriptions_unique"))


@pytest.mark.usefixtures("unique_targets")
def test_a_duplicate_that_slips_past_the_check_is_a_conflict(
    sign_in: SignIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As if a concurrent insert landed between the duplicate check and the commit.
    monkeypatch.setattr(subscriptions, "_check_not_duplicate", lambda *args, **kwargs: None)
    alice = sign_in("alice")
    assert subscribe(alice).status_code == 201
    duplicate = subscribe(alice)
    assert duplicate.status_code == 409
    assert "already have a subscription to AAPL" in duplicate.json()["detail"]
    assert len(alice.get("/subscriptions").json()) == 1


def test_alert_history_is_deleted_with_the_subscription(
    sign_in: SignIn, seeded: Engine, ids: Any
) -> None:
    alice = sign_in("alice")
    subscription_id = subscribe(alice).json()["id"]
    with session_factory(seeded).begin() as session:
        session.add(Notification(subscription_id=subscription_id, accession_no=ids.apple_8k))

    assert alice.delete(f"/subscriptions/{subscription_id}").status_code == 204
    with session_factory(seeded)() as session:
        assert session.scalars(sa.select(Notification)).all() == []
