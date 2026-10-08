"""Liveness for the workers that serve no HTTP: the outbox relay and the consumers.

Kubernetes restarts a container whose process exits, but not one whose process hangs: a loop
stuck on a lost connection looks just as alive. So each worker's main loop keeps a heartbeat
file up to date, and the container's liveness probe runs `filings-healthcheck <name>`, which
fails once the heartbeat is overdue.

The file holds a deadline (Unix time) rather than the time of the last beat: the loop's promise
of when it will beat again. An ordinary beat promises the next one within `window` seconds. Before
a step that may legitimately take longer, the loop promises more: the consumer loop, before each
attempt at a message, allows that attempt's own limit (the summarizer's adds up its LLM
timeouts), so a summarizer working through a long 8-K is never killed mid-summary. The next
ordinary beat after the step shortens the deadline to `window` again, so a loop that hangs
anywhere is caught within `window` seconds of its last promise.

This module is also the probe's command, so it imports nothing heavy: a probe starts a fresh
Python process every few seconds, inside the container's own CPU and memory limits.
"""

import argparse
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

# How long a loop may go between ordinary beats. Generous: the slowest ordinary step, publishing a
# batch while the broker is unreachable or measuring consumer lag, takes about 40 s.
DEFAULT_WINDOW_SECONDS = 120.0


def heartbeat_path(name: str) -> Path:
    """Where the process `name` (e.g. filings-parser) keeps its heartbeat. /tmp in the containers,
    an emptyDir: their root filesystem is read-only."""
    return Path(tempfile.gettempdir()) / f"{name}.heartbeat"


class Heartbeat:
    """The heartbeat file of one process. Create it before anything slow at startup: it removes
    the file a previous container in the same pod may have left, so the probes wait for this
    process's first beat."""

    def __init__(
        self,
        path: Path,
        *,
        window: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._window = window
        self._clock = clock
        self._deadline: float | None = None
        self._failing = False
        path.unlink(missing_ok=True)

    @classmethod
    def for_process(cls, name: str, *, window: float = DEFAULT_WINDOW_SECONDS) -> "Heartbeat":
        return cls(heartbeat_path(name), window=window)

    def beat(self, *, busy_for: float = 0.0) -> None:
        """Promise the next beat within `busy_for` plus the window, from now.

        The file is rewritten only when that moves the deadline by at least half the window, so a
        loop beating every second writes it about once a minute. After an ordinary beat the
        stored deadline is therefore between half a window and one and a half windows ahead.
        """
        deadline = self._clock() + busy_for + self._window
        if self._deadline is not None and abs(deadline - self._deadline) < self._window / 2:
            return
        try:
            temporary = self.path.with_name(f"{self.path.name}.tmp")
            temporary.write_text(f"{deadline:.3f}\n")
            os.replace(temporary, self.path)  # atomic: the probe never reads half a file
        except OSError as exc:
            # Not fatal here: if it persists, the probe fails and Kubernetes restarts the container.
            if not self._failing:
                logger.warning("could not write the heartbeat %s: %s", self.path, exc)
            self._failing = True
            return
        self._failing = False
        self._deadline = deadline


def container_started_at() -> float:
    """When this container's first process started. /tmp is an emptyDir that outlives a container
    restart, so a heartbeat older than this was left by the previous container."""
    try:
        return Path("/proc/1").stat().st_mtime
    except OSError:
        return 0.0


def check(path: Path, *, now: float | None = None, started_at: float | None = None) -> str | None:
    """Why the process that keeps `path` is not live, or None if it is."""
    try:
        written_at = path.stat().st_mtime
        deadline = float(path.read_text())
    except FileNotFoundError:
        return f"no heartbeat yet ({path})"
    except (OSError, ValueError) as exc:
        return f"unreadable heartbeat {path}: {exc}"
    # Until the new process removes it, a previous container's file could pass the startup
    # probe with its old, far-off deadline.
    if written_at < (container_started_at() if started_at is None else started_at):
        return f"no heartbeat yet from this container ({path} is older)"
    now = time.time() if now is None else now
    if now > deadline:
        return f"heartbeat overdue by {now - deadline:.0f}s ({path})"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """`filings-healthcheck <name>`: exit 0 if the process's heartbeat is on time, else 1 with
    the reason on stderr (Kubernetes shows it in the pod's events)."""
    parser = argparse.ArgumentParser(
        prog="filings-healthcheck",
        description="Liveness check for a worker: fails once its heartbeat is overdue.",
    )
    parser.add_argument(
        "name", help="the worker's process name, e.g. filings-parser or filings-outbox-relay"
    )
    args = parser.parse_args(argv)
    problem = check(heartbeat_path(args.name))
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
