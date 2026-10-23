"""Which Form 4s and 8-Ks a subscription wants alerts about (ADR-0012, "Form 4 filters" and
"8-K item filters").

8-Ks: a subscription can list the items it cares about (2.02 results, 5.02 officer changes, ...);
an 8-K is alerted when it reports any of them, and an empty list means every 8-K.

Form 4s: most report routine events (shares withheld for taxes, stock awards, option exercises,
gifts). A subscription can ask for open-market trades only (SEC codes P and S, stock rather than
derivatives: the trades the website and API call insider buying and selling), or purchases only,
and for a minimum dollar value of those trades. Other forms ignore both settings.

A trade's value is shares x price. A price given only in a footnote is unknown; a filing with
such a trade passes the minimum, because missing a large trade is worse than one extra alert.
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, get_args

Form4Trades = Literal["all", "open_market", "purchases"]
FORM4_TRADES: tuple[str, ...] = get_args(Form4Trades)

OPEN_MARKET_CODES = frozenset({"P", "S"})
_CODES: dict[str, frozenset[str]] = {
    "all": OPEN_MARKET_CODES,  # with a minimum value, only open-market trades have one
    "open_market": OPEN_MARKET_CODES,
    "purchases": frozenset({"P"}),
}


@dataclass(frozen=True)
class TradeLine:
    """A non-derivative transaction line of a Form 4 (form4_transactions)."""

    code: str | None
    shares: float | None
    price_per_share: float | None


def wants_form4(trades: str, min_value: int | None, lines: Iterable[TradeLine]) -> bool:
    """Whether a subscription with these settings is alerted about a Form 4 with these
    non-derivative transaction lines."""
    if trades == "all" and min_value is None:
        return True
    relevant = [line for line in lines if line.code in _CODES[trades]]
    if not relevant:
        return False
    if min_value is None:
        return True
    value = 0.0
    for line in relevant:
        if line.shares is None or line.price_per_share is None:
            return True  # value unknown: alert rather than risk missing it
        value += line.shares * line.price_per_share
    return value >= min_value


def wants_8k(items: Collection[str], filing_items: Iterable[str]) -> bool:
    """Whether a subscription interested in `items` (empty: every 8-K) is alerted about an 8-K
    reporting `filing_items`."""
    return not items or any(item in items for item in filing_items)


def alert_reason(
    form_type: str,
    filer_ticker: str,
    *,
    ticker: str | None,
    query: str | None,
    form4_trades: str,
    form4_min_value: int | None,
    form8k_items: Sequence[str],
) -> str | None:
    """Why a subscription sent this filing, for the alert: "Your alert: AAPL · Form 4:
    open-market trades only, $1,000,000 or more", or "Your alert: “buyback” (any watchlist
    company)". Only the filters for this filing's form are named. None for a plain alert on the
    company that filed, which the alert already names."""
    filters = []
    if form_type == "4":
        trades = {"open_market": "open-market trades only", "purchases": "purchases only"}
        parts = [trades[form4_trades]] if form4_trades in trades else []
        if form4_min_value is not None:
            parts.append(f"${form4_min_value:,} or more")
        if parts:
            filters.append(f"Form 4: {', '.join(parts)}")
    if form_type == "8-K" and form8k_items:
        filters.append(f"8-K: {', '.join(form8k_items)}")
    if query is None and not filters and ticker == filer_ticker:
        return None
    subject = " ".join(
        part for part in (ticker, f"“{query}”" if query else None) if part is not None
    )
    if ticker is None:
        subject += " (any watchlist company)"
    return "Your alert: " + " · ".join([subject, *filters])
