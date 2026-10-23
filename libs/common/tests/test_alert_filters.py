from typing import Any

import pytest

from filings_common.alert_filters import (
    FORM4_TRADES,
    TradeLine,
    alert_reason,
    wants_8k,
    wants_form4,
)

SALE = TradeLine("S", 1_000, 250.0)  # $250,000
PURCHASE = TradeLine("P", 100, 50.0)  # $5,000
UNPRICED_SALE = TradeLine("S", 20_000, None)  # the price is only in a footnote
TAX = TradeLine("F", 500, 240.0)  # withheld for taxes: not an open-market trade
AWARD = TradeLine("A", 2_000, None)


@pytest.mark.parametrize(
    ("trades", "min_value", "lines", "wanted"),
    [
        # The default: every Form 4, whatever it reports.
        ("all", None, [TAX], True),
        ("all", None, [], True),
        # Open-market trades only: a tax-withholding or award-only filing is skipped.
        ("open_market", None, [TAX, AWARD], False),
        ("open_market", None, [TAX, SALE], True),
        ("open_market", None, [PURCHASE], True),
        # Purchases only.
        ("purchases", None, [SALE], False),
        ("purchases", None, [SALE, PURCHASE], True),
        # A minimum counts the matching trades' value, shares x price.
        ("open_market", 100_000, [SALE], True),
        ("open_market", 300_000, [SALE], False),
        ("open_market", 255_000, [SALE, PURCHASE], True),  # $250,000 + $5,000
        ("purchases", 10_000, [SALE, PURCHASE], False),  # the sale doesn't count
        # With "all", a minimum still needs open-market trades: only they have a market value.
        ("all", 1, [TAX, AWARD], False),
        ("all", 100_000, [SALE], True),
        # A price only in a footnote: the value is unknown, so the alert isn't risked.
        ("open_market", 10**9, [UNPRICED_SALE], True),
        ("purchases", 1, [UNPRICED_SALE], False),  # still not a purchase
    ],
)
def test_wants_form4(
    trades: str, min_value: int | None, lines: list[TradeLine], wanted: bool
) -> None:
    assert wants_form4(trades, min_value, lines) is wanted


def test_the_choices_match_the_database_check() -> None:
    # subscriptions.form4_trades has CHECK (form4_trades IN ('all', 'open_market', 'purchases')).
    assert FORM4_TRADES == ("all", "open_market", "purchases")


@pytest.mark.parametrize(
    ("items", "filing_items", "wanted"),
    [
        ([], ["5.02", "9.01"], True),  # no items chosen: every 8-K
        (["2.02"], ["2.02", "9.01"], True),
        (["2.02", "5.02"], ["5.02"], True),  # any one of them is enough
        (["2.02"], ["5.02", "9.01"], False),
        (["2.02"], [], False),
    ],
)
def test_wants_8k(items: list[str], filing_items: list[str], wanted: bool) -> None:
    assert wants_8k(items, filing_items) is wanted


PLAIN: dict[str, Any] = {
    "query": None,
    "form4_trades": "all",
    "form4_min_value": None,
    "form8k_items": [],
}


@pytest.mark.parametrize(
    ("form_type", "settings", "reason"),
    [
        # A plain alert on the filer: the alert already says which company.
        ("4", {"ticker": "AAPL"}, None),
        # Filed by someone else about the company followed (the issuer).
        ("4", {"ticker": "LEN"}, "Your alert: LEN"),
        (
            "4",
            {"ticker": "AAPL", "form4_trades": "open_market", "form4_min_value": 1_000_000},
            "Your alert: AAPL \u00b7 Form 4: open-market trades only, $1,000,000 or more",
        ),
        (
            "4",
            {"ticker": "AAPL", "form4_trades": "purchases"},
            "Your alert: AAPL \u00b7 Form 4: purchases only",
        ),
        # Only this form's filters: an 8-K isn't told about the Form 4 settings, nor a Form 4 about
        # the 8-K items.
        ("8-K", {"ticker": "AAPL", "form4_trades": "purchases"}, None),
        ("4", {"ticker": "AAPL", "form8k_items": ["2.02"]}, None),
        (
            "8-K",
            {"ticker": "AAPL", "form8k_items": ["2.02", "5.02"]},
            "Your alert: AAPL \u00b7 8-K: 2.02, 5.02",
        ),
        (
            "8-K",
            {"ticker": None, "query": "buyback"},
            "Your alert: \u201cbuyback\u201d (any watchlist company)",
        ),
        ("8-K", {"ticker": "AAPL", "query": "buyback"}, "Your alert: AAPL \u201cbuyback\u201d"),
    ],
)
def test_alert_reason(form_type: str, settings: dict[str, Any], reason: str | None) -> None:
    assert alert_reason(form_type, "AAPL", **(PLAIN | settings)) == reason
