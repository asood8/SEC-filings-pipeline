import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from filings_discovery import cli
from filings_discovery.cli import main


@pytest.fixture(autouse=True)
def restore_sigterm() -> Iterator[None]:
    """main() installs a SIGTERM handler: keep it out of the other tests."""
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_missing_watchlist_fails_before_any_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCOVERY_WATCHLIST_PATH", str(tmp_path / "missing.toml"))
    assert main(["load-companies"]) == 2


class FakeTelemetry:
    def __init__(self) -> None:
        self.shut_down = False
        self.sigterm_handler_while_exporting: object = None

    def shutdown(self) -> None:
        self.shut_down = True
        self.sigterm_handler_while_exporting = signal.getsignal(signal.SIGTERM)


def test_sigterm_stops_a_run_but_still_exports_its_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = FakeTelemetry()
    monkeypatch.setattr(cli, "setup_telemetry", lambda name: telemetry)

    def slow_run(command: str) -> int:
        os.kill(os.getpid(), signal.SIGTERM)  # as Kubernetes at the job's deadline
        time.sleep(5)  # interrupted by the handler
        raise AssertionError("SIGTERM did not stop the run")

    monkeypatch.setattr(cli, "_run", slow_run)
    start = time.monotonic()
    with pytest.raises(SystemExit) as exited:
        main(["run"])
    assert exited.value.code == 128 + signal.SIGTERM
    assert time.monotonic() - start < 2
    assert telemetry.shut_down  # the spans buffered so far were exported...
    # ...and a second SIGTERM couldn't have interrupted that (it is ignored, PID 1 or not).
    assert telemetry.sigterm_handler_while_exporting == signal.SIG_IGN
