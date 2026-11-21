"""The nightly backup's last step (`filings-backup-report`): report the dump that was uploaded.

The backup job (deploy/k8s/base/postgres-backup.yaml) dumps Postgres, uploads the dump to R2, and
only then runs this, so a report means the night's backup is in R2. It records the dump's size as
the `filings.backup.size` gauge. Two alerts (deploy/grafana/alerting/filings.yaml) read it: one
when no backup has been reported for over a day, and one when a backup is under half the size of
the largest in the past week. That second one catches a backup that "works" but holds the wrong
thing, such as a database recreated empty after its volume was lost: without it, the nightly
empty dumps would look healthy while the bucket's lifecycle rule deletes the last good one.

The job passes the tables whose rows pg_dump left out (`--excluded-data`, the same names as its
`--exclude-table-data`), and the size is labeled with them: dumps that leave out different tables
aren't comparable, so the second alert compares only dumps labeled alike. Leaving the search
embeddings out (2026-11-21) made the dumps much smaller without anything having been lost.
"""

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from opentelemetry.util.types import AttributeValue

from filings_common import metrics
from filings_common.telemetry import setup_telemetry

logger = logging.getLogger(__name__)


def report(dump: Path, excluded_data: Sequence[str] = ()) -> int:
    """Record the dump's size, labeled with the tables whose rows it left out; 1 (and nothing
    recorded) if it is missing or empty."""
    try:
        size = dump.stat().st_size
    except OSError as exc:
        logger.error("no dump to report at %s: %r", dump, exc)
        return 1
    if size == 0:
        logger.error("the dump at %s is empty", dump)
        return 1
    attributes: dict[str, AttributeValue] = {}
    if excluded_data:
        attributes[metrics.BACKUP_EXCLUDED_DATA] = ",".join(sorted(set(excluded_data)))
    metrics.BACKUP_SIZE_VALUES.replace(report, [(size, attributes)])
    left_out = f" (without the rows of {', '.join(sorted(set(excluded_data)))})"
    logger.info("backup reported: %.1f MB%s", size / 10**6, left_out if excluded_data else "")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filings-backup-report", description=__doc__.split("\n")[0]
    )
    parser.add_argument("dump", type=Path, help="the dump file the backup uploaded")
    parser.add_argument(
        "--excluded-data",
        action="append",
        default=[],
        metavar="TABLE",
        help="a table whose rows the dump left out (pg_dump --exclude-table-data); repeatable",
    )
    args = parser.parse_args(argv)
    telemetry = setup_telemetry("filings-backup-report", distribution="filings-common")
    try:
        return report(args.dump, args.excluded_data)
    finally:
        telemetry.shutdown()  # a job exits right after: export the gauge first
