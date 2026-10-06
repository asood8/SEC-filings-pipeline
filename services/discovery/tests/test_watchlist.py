from pathlib import Path

import pytest

from filings_discovery.watchlist import WatchlistError, load_watchlist, parse_watchlist


def test_normalizes_and_deduplicates_in_file_order() -> None:
    text = 'tickers = ["msft", " AAPL ", "BRK.B", "MSFT", "brk-b"]'
    assert parse_watchlist(text) == ["MSFT", "AAPL", "BRK-B"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "tickers = []",
        'tickers = "AAPL"',
        'tickers = ["AAPL", ""]',
        "tickers = [1, 2]",
        "tickers = [",
    ],
)
def test_rejects_invalid_watchlists(text: str) -> None:
    with pytest.raises(WatchlistError):
        parse_watchlist(text)


def test_missing_file_points_at_the_example(tmp_path: Path) -> None:
    with pytest.raises(WatchlistError, match=r"watchlist\.example\.toml"):
        load_watchlist(tmp_path / "watchlist.toml")


def test_example_watchlist_is_valid() -> None:
    example = Path(__file__).parents[3] / "config" / "watchlist.example.toml"
    assert load_watchlist(example)
