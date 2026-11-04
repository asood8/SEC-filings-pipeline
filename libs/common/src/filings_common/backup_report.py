"""The nightly backup's last step (`filings-backup-report`): report the dump that was uploaded.

The backup job (deploy/k8s/base/postgres-backup.yaml) dumps Postgres, uploads the dump to R2, and
only then runs this, so a report means the night's backup is in R2. It records the dump's size as
the `filings.backup.size` gauge. Two alerts (deploy/grafana/alerting/filings.yaml) read it: one
when no backup has been reported for over a day, and one when a backup is under half the size of
the largest in the past week. That second one catches a backup that "works" but holds the wrong
thing, such as a database recreated empty after its volume was lost: without it, the nightly
empty dumps would look healthy while the bucket's lifecycle rule deletes the last good one.
"""

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from filings_common import metrics
from filings_common.telemetry import setup_telemetry

logger = logging.getLogger(__name__)


def report(dump: Path) -> int:
    """Record the dump's size; 1 (and nothing recorded) if it is missing or empty."""
    try:
        size = dump.stat().st_size
    except OSError as exc:
        logger.error("no dump to report at %s: %r", dump, exc)
        return 1
    if size == 0:
        logger.error("the dump at %s is empty", dump)
        return 1
    metrics.BACKUP_SIZE_VALUES.replace(report, [(size, {})])
    logger.info("backup reported: %.1f MB", size / 10**6)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filings-backup-report", description=__doc__.split("\n")[0]
    )
    parser.add_argument("dump", type=Path, help="the dump file the backup uploaded")
    args = parser.parse_args(argv)
    telemetry = setup_telemetry("filings-backup-report", distribution="filings-common")
    try:
        return report(args.dump)
    finally:
        telemetry.shutdown()  # a job exits right after: export the gauge first
