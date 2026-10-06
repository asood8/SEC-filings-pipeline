"""The watchlist: which companies discovery polls, as a TOML file of tickers.

tickers = ["AAPL", "MSFT", "BRK-B"]
"""

import tomllib
from pathlib import Path


class WatchlistError(ValueError):
    pass


def normalize_ticker(ticker: str) -> str:
    # SEC writes share classes with a dash (BRK-B); people often write BRK.B.
    return ticker.strip().upper().replace(".", "-")


def parse_watchlist(text: str) -> list[str]:
    """Tickers in file order, normalized, without duplicates."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise WatchlistError(f"watchlist is not valid TOML: {exc}") from exc
    tickers = data.get("tickers")
    if not isinstance(tickers, list) or not tickers:
        raise WatchlistError("watchlist needs a non-empty `tickers` list")
    if not all(isinstance(t, str) and t.strip() for t in tickers):
        raise WatchlistError("every watchlist ticker must be a non-empty string")
    return list(dict.fromkeys(normalize_ticker(t) for t in tickers))


def load_watchlist(path: Path) -> list[str]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise WatchlistError(
            f"watchlist not found at {path}; see config/watchlist.example.toml"
        ) from None
    return parse_watchlist(text)
