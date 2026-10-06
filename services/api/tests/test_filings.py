from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api.app import create_app
from filings_common.db import Company, Filing, session_factory

pytestmark = pytest.mark.integration


@pytest.fixture
def client(clean_db: Engine) -> Iterator[TestClient]:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        session.add_all(
            [
                Company(cik=320193, ticker="AAPL", name="Apple Inc.", on_watchlist=True),
                Company(cik=789019, ticker="MSFT", name="MICROSOFT CORP", on_watchlist=True),
            ]
        )
        session.flush()
        for accession_no, cik, form, filed in [
            ("0000320193-26-000001", 320193, "8-K", date(2026, 9, 28)),
            ("0001140361-26-000002", 320193, "4", date(2026, 9, 30)),
            ("0000789019-26-000003", 789019, "8-K", date(2026, 9, 29)),
        ]:
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=cik,
                    form_type=form,
                    filed_at=filed,
                    primary_doc_url=f"https://www.sec.gov/Archives/{accession_no}.htm",
                    raw_object_key=f"filings/{cik}/{accession_no}/doc.htm",
                )
            )
    with TestClient(create_app(sessions)) as test_client:
        yield test_client


def accessions(response_json: list[dict[str, object]]) -> list[object]:
    return [f["accession_no"] for f in response_json]


def test_lists_latest_filings_first_with_company_details(client: TestClient) -> None:
    response = client.get("/filings")
    assert response.status_code == 200
    body = response.json()
    assert accessions(body) == [
        "0001140361-26-000002",
        "0000789019-26-000003",
        "0000320193-26-000001",
    ]
    assert body[0] | {"accepted_at": None} == {
        "accession_no": "0001140361-26-000002",
        "cik": 320193,
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "form_type": "4",
        "filed_at": "2026-09-30",
        "accepted_at": None,
        "status": "discovered",
        "sec_url": "https://www.sec.gov/Archives/0001140361-26-000002.htm",
    }


def test_filters_by_company_and_form_type(client: TestClient) -> None:
    by_company = client.get("/filings", params={"cik": 320193}).json()
    assert accessions(by_company) == ["0001140361-26-000002", "0000320193-26-000001"]
    by_both = client.get("/filings", params={"cik": 320193, "form_type": "8-K"}).json()
    assert accessions(by_both) == ["0000320193-26-000001"]


def test_limit_is_bounded(client: TestClient) -> None:
    assert len(client.get("/filings", params={"limit": 1}).json()) == 1
    assert client.get("/filings", params={"limit": 0}).status_code == 422
    assert client.get("/filings", params={"limit": 101}).status_code == 422


def test_health_check(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
