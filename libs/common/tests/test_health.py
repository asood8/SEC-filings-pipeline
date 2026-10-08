import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from filings_common import health
from filings_common.health import Heartbeat, check, heartbeat_path


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def deadline(path: Path) -> float:
    return float(path.read_text())


def test_a_beat_promises_the_next_within_the_window(tmp_path: Path) -> None:
    clock = Clock()
    heartbeat = Heartbeat(tmp_path / "w.heartbeat", window=60, clock=clock)
    heartbeat.beat()
    assert deadline(heartbeat.path) == clock.now + 60
    assert check(heartbeat.path, now=clock.now + 60) is None
    assert check(heartbeat.path, now=clock.now + 61) == (
        f"heartbeat overdue by 1s ({heartbeat.path})"
    )


def test_frequent_beats_rewrite_the_file_only_every_half_window(tmp_path: Path) -> None:
    clock = Clock()
    heartbeat = Heartbeat(tmp_path / "w.heartbeat", window=60, clock=clock)
    start = clock.now
    written = []
    for _ in range(90):  # one beat a second, as the consume loop's polls
        heartbeat.beat()
        written.append(deadline(heartbeat.path))
        clock.now += 1
    assert sorted(set(written)) == [start + 60, start + 90, start + 120]
    # The stored deadline always stays at least half a window ahead.
    assert all(d - (start + i) >= 30 for i, d in enumerate(written))


def test_a_long_step_is_allowed_for_then_the_deadline_shrinks_back(tmp_path: Path) -> None:
    clock = Clock()
    heartbeat = Heartbeat(tmp_path / "w.heartbeat", window=60, clock=clock)
    heartbeat.beat()
    heartbeat.beat(busy_for=720)  # e.g. an attempt at summarizing an 8-K
    assert deadline(heartbeat.path) == clock.now + 720 + 60
    assert check(heartbeat.path, now=clock.now + 700) is None  # still working: not restarted
    clock.now += 100
    heartbeat.beat()  # the step ended: an ordinary beat
    assert deadline(heartbeat.path) == clock.now + 60  # a hang from here is caught in a window


def test_a_file_left_by_a_previous_container_is_removed_at_start(tmp_path: Path) -> None:
    path = tmp_path / "w.heartbeat"
    path.write_text("9999999999.0\n")  # e.g. killed while busy, its deadline still ahead
    Heartbeat(path)
    assert check(path) == f"no heartbeat yet ({path})"


def test_a_failed_write_is_logged_once_and_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    heartbeat = Heartbeat(tmp_path / "missing-dir" / "w.heartbeat", window=60, clock=clock)
    for _ in range(3):
        heartbeat.beat()
        clock.now += 60
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert check(heartbeat.path) is not None  # so the probe fails, and Kubernetes restarts it


def test_an_unreadable_heartbeat_is_not_live(tmp_path: Path) -> None:
    path = tmp_path / "w.heartbeat"
    path.write_text("not a number")
    problem = check(path)
    assert problem is not None
    assert problem.startswith(f"unreadable heartbeat {path}")


def test_heartbeats_live_in_the_temporary_directory_by_process_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert heartbeat_path("filings-parser") == tmp_path / "filings-parser.heartbeat"
    assert Heartbeat.for_process("filings-parser").path == tmp_path / "filings-parser.heartbeat"


def test_the_check_command_exits_nonzero_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert health.main(["filings-parser"]) == 1
    assert "no heartbeat yet" in capsys.readouterr().err

    Heartbeat.for_process("filings-parser").beat()
    assert health.main(["filings-parser"]) == 0
    assert capsys.readouterr().err == ""


def test_the_probe_command_is_installed_and_starts_light(tmp_path: Path) -> None:
    """The probe runs as a new process every few seconds in the worker's container."""
    script = Path(sys.executable).parent / "filings-healthcheck"
    (tmp_path / "filings-notifier.heartbeat").write_text("9999999999.0\n")
    env = {"TMPDIR": str(tmp_path)}
    live = subprocess.run([script, "filings-notifier"], env=env, capture_output=True, text=True)
    assert (live.returncode, live.stderr) == (0, "")
    hung = subprocess.run([script, "filings-outbox-relay"], env=env, capture_output=True, text=True)
    assert hung.returncode == 1
    assert "no heartbeat yet" in hung.stderr
    # Nothing heavy is imported: no SQLAlchemy, Kafka client, or OpenTelemetry.
    modules = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, filings_common.health; print(' '.join(sorted(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    heavy = {"sqlalchemy", "confluent_kafka", "opentelemetry", "pydantic"}
    assert [m for m in modules if m.split(".")[0] in heavy] == []


def test_a_heartbeat_from_before_this_container_started_is_not_live(tmp_path: Path) -> None:
    """The emptyDir outlives a restart; the old file must not pass the new startup probe."""
    path = tmp_path / "summarizer.heartbeat"
    path.write_text(f"{time.time() + 900:.3f}\n")  # an old, far-off deadline
    written = path.stat().st_mtime
    assert health.check(path, started_at=written + 1) == (
        f"no heartbeat yet from this container ({path} is older)"
    )
    assert health.check(path, started_at=written - 1) is None
