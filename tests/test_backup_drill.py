"""The backup restore drill (`scripts/backup-drill.sh`, `make backup-drill`) on a throwaway
database with the real schema: the dump restores, every table matches, and nothing is left behind.

The drill runs its tools inside the Compose stack's postgres container, so these tests need Docker
as well as the database.
"""

import os
import re
import subprocess
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.db import (
    EMBEDDING_DIMENSIONS,
    Company,
    Filing,
    FilingEmbedding,
    session_factory,
)

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).parents[1] / "scripts" / "backup-drill.sh"
APPLE = 320193


@pytest.fixture(scope="module")
def _postgres_container() -> None:
    """Skips (or fails, with REQUIRE_SERVICES=1) unless the Compose postgres container runs."""
    project = os.environ.get("COMPOSE_PROJECT_NAME", "sec-filings")
    try:
        found = subprocess.run(
            [
                "docker",
                "ps",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=postgres",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        found, reason = "", f"Docker is not usable ({exc.__class__.__name__})"
    else:
        reason = "the Compose postgres container is not running"
    if not found:
        message = f"{reason}; start the stack with `make up`"
        if os.environ.get("REQUIRE_SERVICES") == "1":
            pytest.fail(message)
        pytest.skip(message)


def _drill(database: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), database], capture_output=True, text=True, timeout=300, check=False
    )


def _database_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        query = sa.text("SELECT count(*) FROM pg_database WHERE datname = :name")
        return bool(conn.scalar(query, {"name": name}))


@pytest.mark.usefixtures("_postgres_container")
def test_drill_restores_every_table_and_cleans_up(clean_db: Engine) -> None:
    with session_factory(clean_db).begin() as session:
        session.add(Company(cik=APPLE, ticker="AAPL", name="Apple Inc.", on_watchlist=True))
        session.flush()
        for n in range(3):
            accession_no = f"0000320193-26-00000{n}"
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=APPLE,
                    form_type="8-K",
                    filed_at=date(2026, 10, 1 + n),
                    primary_doc_url=f"https://www.sec.gov/{accession_no}.htm",
                    raw_object_key=f"raw/{accession_no}.htm",
                )
            )
            session.flush()
            session.add(
                FilingEmbedding(
                    accession_no=accession_no,
                    model="test",
                    embedding=[0.25 * n] * EMBEDDING_DIMENSIONS,
                )
            )
    database = clean_db.url.database
    assert database is not None

    result = _drill(database)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED" in result.stdout
    assert re.search(r"public\.filings\s+3\s+same", result.stdout)
    assert re.search(r"public\.filing_embeddings\s+3\s+same", result.stdout)
    assert re.search(r"public\.alembic_version\s+1\s+same", result.stdout)
    restored = re.search(r"restored into (\w+)", result.stdout)
    assert restored is not None
    assert restored[1] != database
    assert not _database_exists(clean_db, restored[1])
    assert "Deleted the dump." in result.stdout


@pytest.mark.usefixtures("_postgres_container")
def test_drill_fails_cleanly_without_its_source() -> None:
    missing = f"filings_test_missing_{uuid.uuid4().hex[:12]}"

    result = _drill(missing)

    assert result.returncode != 0
    assert f'"{missing}" stopped early' in result.stderr
    assert "restored into" not in result.stdout
