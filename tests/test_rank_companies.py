"""scripts/rank_companies.py: how watchlist additions are ranked and filtered (no SEC calls)."""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "rank_companies.py"
    spec = importlib.util.spec_from_file_location("rank_companies", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rank_companies = _load()


def row(cik: int, value: float, name: str = "X") -> dict[str, Any]:
    return {"cik": cik, "val": value, "entityName": name}


def company(state: str = "TX", forms: tuple[str, ...] = ("10-K", "8-K", "4")) -> dict[str, Any]:
    return {
        "name": "Example Corp",
        "tickers": ["EXM"],
        "business": {"stateOrCountry": state, "city": "Somewhere"},
        "forms": list(forms),
    }


def test_revenue_is_the_largest_tag_or_a_banks_gross_revenue() -> None:
    current = {
        "Revenues": [row(1, 10e9, "Industrial"), row(2, 8e9, "Bank")],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [row(1, 12e9, "Industrial")],
        # The bank's interest income plus its noninterest income beats its net revenue.
        "InterestAndDividendIncomeOperating": [row(2, 20e9)],
        "InterestAndFeeIncomeLoansAndLeases": [row(2, 19e9)],
        "NoninterestIncome": [row(2, 5e9, "Bank"), row(3, 1e9, "No interest income")],
    }
    previous = {"Revenues": [row(1, 99e9), row(4, 7e9, "New holding company")]}
    assert rank_companies.revenues(current, previous) == {
        1: (12e9, "Industrial"),  # this year's, though last year's was larger
        2: (25e9, "Bank"),
        4: (7e9, "New holding company"),  # no frame this year: last year's
    }


@pytest.mark.parametrize(
    ("cik", "details", "reason"),
    [
        (1, company(state="L2"), "headquartered outside the US"),
        (1, company(forms=("20-F", "6-K", "4")), "a foreign issuer"),
        (1, company(forms=("10-K", "8-K")), "no Form 4s"),
        (1855447, company(), "scaling error"),
    ],
)
def test_reasons_a_company_is_left_out(cik: int, details: dict[str, Any], reason: str) -> None:
    assert reason in "; ".join(rank_companies.reasons_left_out(cik, details))


def test_a_us_company_filing_10ks_and_form_4s_qualifies() -> None:
    assert rank_companies.reasons_left_out(1, company()) == []


def test_ranking_skips_the_watchlist_and_fetches_only_what_it_needs() -> None:
    revenue = {cik: (float(100 - cik), f"Company {cik}") for cik in range(1, 8)}
    listed = {cik: [f"T{cik}"] for cik in range(1, 7)}  # 7 has no US listing
    fetched: list[int] = []

    def profile(cik: int) -> dict[str, Any]:
        fetched.append(cik)
        return company(state="V8" if cik == 3 else "NY")

    chosen, left_out = rank_companies.rank(revenue, listed, {1}, profile, count=2)
    assert [cik for cik, _, _ in chosen] == [2, 4]
    assert [(cik, reasons[0][:12]) for cik, _, _, reasons in left_out] == [(3, "headquartere")]
    assert fetched == [2, 3, 4]  # not 5 or 6: two already qualified


def test_watchlist_tickers_map_to_ciks() -> None:
    listed = rank_companies.listed_tickers(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [1, "A", "AAA", "Nasdaq"],
                [2, "B", "BBB", "NYSE"],
                [2, "B", "BBBP", "OTC"],
                [3, "C", "CCC", "OTC"],
            ],
        }
    )
    assert listed == {1: ["AAA"], 2: ["BBB"]}
    assert rank_companies.watchlist_ciks(listed, ["BBB", "ZZZ"]) == {2}
