"""Run the whole pipeline locally (`make pipeline`): every stage until Ctrl+C, and discovery every
few minutes.

In production each stage is its own workload and discovery runs on a schedule; this is the local
stand-in. Output from all stages is interleaved, each line prefixed with its stage.
"""

import argparse
import signal
import subprocess
import sys
import threading

STAGES = {
    "relay": "filings-outbox-relay",
    "parser": "filings-parser",
    "summarizer": "filings-summarizer",
    "notifier": "filings-notifier",
}


def _forward(name: str, process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        print(f"{name:<10} | {line}", end="", flush=True)


def _start(name: str, command: list[str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=_forward, args=(name, process), daemon=True).start()
    return process


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--every", type=float, default=10.0, help="minutes between discovery runs (default 10)"
    )
    args = parser.parse_args()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    stages = {name: _start(name, [command]) for name, command in STAGES.items()}
    try:
        while not stop.is_set():
            _start("discovery", ["filings-discovery", "run"]).wait()
            if dead := [name for name, process in stages.items() if process.poll() is not None]:
                print(f"stopping: {', '.join(dead)} exited", file=sys.stderr)
                return 1
            stop.wait(args.every * 60)
    finally:
        for process in stages.values():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in stages.values():
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
