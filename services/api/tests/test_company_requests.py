"""Company suggestions from signed-in users, and the admin's review."""

from collections.abc import Callable
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api.admin_cli import main as admin_main
from filings_api.auth import current_user
from filings_api.queries import SessionDep
from filings_api.routes.company_requests import MAX_OPEN_REQUESTS
from filings_common.db import Company, User, session_factory

pytestmark = pytest.mark.integration

ClientFactory = Callable[..., TestClient]
SignIn = Callable[[str], TestClient]


@pytest.fixture
def users(seeded: Engine) -> dict[str, int]:
    with session_factory(seeded).begin() as session:
        people = {
            "ada": User(email="ada@example.com", google_sub="g-ada"),
            "bo": User(email="bo@example.com", google_sub="g-bo"),
            "admin": User(email="owner@example.com", google_sub="g-owner", is_admin=True),
        }
        session.add_all(people.values())
        session.add_all(
            [
                Company(cik=1373715, ticker="NOW", name="ServiceNow, Inc.", on_watchlist=False),
                Company(
                    cik=1543151, ticker="UBER", name="Uber Technologies, Inc", on_watchlist=False
                ),
                *[
                    Company(
                        cik=9_000_000 + n, ticker=f"T{n:02d}", name=f"Test {n}", on_watchlist=False
                    )
                    for n in range(MAX_OPEN_REQUESTS + 1)
                ],
            ]
        )
        session.flush()
        return {name: user.id for name, user in people.items()}


@pytest.fixture
def sign_in(make_client: ClientFactory, users: dict[str, int]) -> SignIn:
    def build(name: str) -> TestClient:
        client = make_client()
        user_id = users[name]

        def signed_in(session: SessionDep) -> User:
            return session.get_one(User, user_id)

        cast(FastAPI, client.app).dependency_overrides[current_user] = signed_in
        return client

    return build


def test_a_signed_in_user_suggests_a_company_and_sees_its_status(sign_in: SignIn) -> None:
    ada = sign_in("ada")
    created = ada.post("/company-requests", json={"ticker": "now", "note": "  Big in   AI  "})
    assert created.status_code == 201, created.text
    assert created.headers["Cache-Control"] == "no-store"
    body = created.json()
    assert body["company"] == {"cik": 1373715, "ticker": "NOW", "name": "ServiceNow, Inc."}
    assert (body["note"], body["status"], body["decided_at"]) == ("Big in AI", "open", None)
    assert [r["company"]["ticker"] for r in ada.get("/company-requests").json()] == ["NOW"]
    assert sign_in("bo").get("/company-requests").json() == []  # only your own


@pytest.mark.parametrize(
    ("ticker", "status", "detail"),
    [
        ("AAPL", 409, "AAPL is already followed"),
        ("NOPE", 422, "SEC knows no company with ticker NOPE"),
    ],
)
def test_followed_and_unknown_companies_are_refused(
    sign_in: SignIn, ticker: str, status: int, detail: str
) -> None:
    response = sign_in("ada").post("/company-requests", json={"ticker": ticker})
    assert (response.status_code, response.json()["detail"]) == (status, detail)


def test_one_suggestion_per_company_and_a_cap_on_open_ones(sign_in: SignIn) -> None:
    ada = sign_in("ada")
    assert ada.post("/company-requests", json={"ticker": "UBER"}).status_code == 201
    again = ada.post("/company-requests", json={"ticker": "uber"})
    assert (again.status_code, again.json()["detail"]) == (409, "you already suggested UBER")
    for n in range(MAX_OPEN_REQUESTS - 1):
        assert ada.post("/company-requests", json={"ticker": f"T{n:02d}"}).status_code == 201
    over = ada.post("/company-requests", json={"ticker": f"T{MAX_OPEN_REQUESTS:02d}"})
    assert over.status_code == 409
    assert "10 open suggestions" in over.json()["detail"]


def test_signing_in_is_required(make_client: ClientFactory, users: dict[str, int]) -> None:
    client = make_client()
    assert client.post("/company-requests", json={"ticker": "NOW"}).status_code == 401
    assert client.get("/admin/company-requests").status_code == 401


def test_the_admin_sees_suggestions_by_company_and_decides(sign_in: SignIn, seeded: Engine) -> None:
    ada, bo, admin = sign_in("ada"), sign_in("bo"), sign_in("admin")
    ada.post("/company-requests", json={"ticker": "UBER", "note": "Rides and delivery"})
    bo.post("/company-requests", json={"ticker": "UBER"})
    bo.post("/company-requests", json={"ticker": "NOW"})

    assert ada.get("/admin/company-requests").status_code == 403  # admins only
    listed = admin.get("/admin/company-requests").json()
    assert [(s["company"]["ticker"], s["requests"]) for s in listed] == [("UBER", 2), ("NOW", 1)]
    assert listed[0]["notes"][0]["note"] == "Rides and delivery"
    assert listed[0]["notes"][0]["by"] == "a***@example.com"  # masked
    assert listed[0]["on_watchlist"] is False

    early = admin.patch("/admin/company-requests/uber", json={"status": "added"})
    assert (early.status_code, early.json()["detail"]) == (
        409,
        "UBER isn't on the watchlist yet: add it there first",
    )
    with session_factory(seeded).begin() as session:  # the watchlist change, then load-companies
        session.get_one(Company, 1543151).on_watchlist = True
    decided = admin.patch("/admin/company-requests/uber", json={"status": "added"})
    assert decided.status_code == 200
    assert (decided.json()["status"], decided.json()["requests"]) == ("added", 2)
    assert [s["company"]["ticker"] for s in admin.get("/admin/company-requests").json()] == ["NOW"]
    added = admin.get("/admin/company-requests", params={"status": "added"}).json()
    assert [s["company"]["ticker"] for s in added] == ["UBER"]
    mine = {r["company"]["ticker"]: r for r in bo.get("/company-requests").json()}
    assert mine["UBER"]["status"] == "added"
    assert mine["UBER"]["decided_at"] is not None
    assert mine["NOW"]["status"] == "open"
    assert ada.patch("/admin/company-requests/NOW", json={"status": "declined"}).status_code == 403
    assert admin.patch("/admin/company-requests/AAPL", json={"status": "added"}).status_code == 404


def test_me_says_who_is_an_admin(sign_in: SignIn) -> None:
    assert sign_in("admin").get("/me").json()["is_admin"] is True
    assert sign_in("ada").get("/me").json()["is_admin"] is False


def test_filings_admin_grants_and_revokes(
    users: dict[str, int],
    seeded: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", seeded.url.render_as_string(hide_password=False))
    assert admin_main(["grant", "ADA@example.com"]) == 0
    assert "a***@example.com is now an admin" in capsys.readouterr().out
    with session_factory(seeded)() as session:
        assert session.get_one(User, users["ada"]).is_admin is True
    assert admin_main(["revoke", "ada@example.com"]) == 0
    assert admin_main(["grant", "nobody@example.com"]) == 1
    assert "sign in on the website first" in capsys.readouterr().err
