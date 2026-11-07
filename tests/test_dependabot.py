"""Dependabot's configuration, .github/dependabot.yml (docs/adr/0023): every lock file in the
repository has an entry, so a new one isn't left out by accident, and every entry waits a week
before proposing a release."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
CONFIG = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())

# Which ecosystem reads each kind of lock file.
LOCKS = {"uv.lock": "uv", "package-lock.json": "npm", ".terraform.lock.hcl": "terraform"}


def covered() -> set[tuple[str, str]]:
    pairs = set()
    for update in CONFIG["updates"]:
        for directory in update.get("directories", [update.get("directory")]):
            pairs.add((update["package-ecosystem"], directory.rstrip("/") or "/"))
    return pairs


def test_every_lock_file_has_an_entry() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    locks = [Path(p) for p in tracked if Path(p).name in LOCKS]
    assert locks, "no lock files found"
    for lock in locks:
        directory = "/" + str(lock.parent) if str(lock.parent) != "." else "/"
        assert (LOCKS[lock.name], directory) in covered(), f"{lock} has no Dependabot entry"


def test_workflow_actions_are_updated() -> None:
    assert ("github-actions", "/") in covered()


def test_every_entry_waits_a_week_and_runs_weekly() -> None:
    for update in CONFIG["updates"]:
        assert update["cooldown"]["default-days"] >= 7, update["package-ecosystem"]
        assert update["schedule"]["interval"] == "weekly", update["package-ecosystem"]
