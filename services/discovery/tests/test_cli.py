from pathlib import Path

import pytest

from filings_discovery.cli import main


def test_missing_watchlist_fails_before_any_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCOVERY_WATCHLIST_PATH", str(tmp_path / "missing.toml"))
    assert main(["load-companies"]) == 2
