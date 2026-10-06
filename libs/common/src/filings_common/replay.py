"""Re-announce filings from Postgres through the outbox: `filings-replay discovered | parsed`.

- `discovered` re-publishes `filings.discovered`, so the parser parses the filings again: after a
  new parser is added or a parser version changes. Parsing is idempotent. A filing that is already
  `parsed` is updated in place but not announced again.
- `parsed` re-publishes `filings.parsed`, so the summarizer summarizes again: after a prompt or
  model change.

Events go through the outbox, so they are published by the relay with the usual at-least-once
guarantee. Consumers are idempotent, so replaying is always safe.
"""

import argparse
import logging
from collections.abc import Sequence
from itertools import batched
from typing import Any

import sqlalchemy as sa
from pymongo import MongoClient
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import DatabaseSettings, MongoSettings
from filings_common.db import Filing, FilingStatus, create_engine, session_factory
from filings_common.events import FilingDiscovered, FilingParsed, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.outbox import enqueue

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
# Statuses a filing has once it has been parsed.
PARSED_OR_LATER = (FilingStatus.PARSED, FilingStatus.READY, FilingStatus.NOTIFIED)


def _filings(
    session: Session, *, form_type: str | None, statuses: Sequence[FilingStatus] | None
) -> list[Filing]:
    query = sa.select(Filing).order_by(Filing.filed_at, Filing.accession_no)
    if form_type is not None:
        query = query.where(Filing.form_type == form_type)
    if statuses is not None:
        query = query.where(Filing.status.in_(statuses))
    return list(session.scalars(query))


def replay_discovered(
    sessions: sessionmaker[Session],
    *,
    form_type: str | None = None,
    statuses: Sequence[FilingStatus] | None = None,
) -> int:
    """Enqueue `filings.discovered` for matching filings. Returns how many."""
    with sessions() as session:
        filings = _filings(session, form_type=form_type, statuses=statuses)
    for batch in batched(filings, BATCH_SIZE):
        with sessions.begin() as session:
            for filing in batch:
                event = FilingDiscovered(
                    accession_no=filing.accession_no,
                    cik=filing.cik,
                    form_type=filing.form_type,
                    filed_at=filing.filed_at,
                    raw_object_key=filing.raw_object_key,
                )
                enqueue(session, Topic.FILINGS_DISCOVERED, event)
    return len(filings)


def replay_parsed(
    sessions: sessionmaker[Session],
    parsed: ParsedFilingStore,
    *,
    form_type: str | None = None,
) -> int:
    """Enqueue `filings.parsed` for parsed filings. Returns how many.

    The parser version comes from the stored payload; filings with no payload are skipped.
    """
    with sessions() as session:
        filings = _filings(session, form_type=form_type, statuses=PARSED_OR_LATER)
    count = 0
    for batch in batched(filings, BATCH_SIZE):
        versions = parsed.parser_versions([f.accession_no for f in batch])
        with sessions.begin() as session:
            for filing in batch:
                version = versions.get(filing.accession_no)
                if version is None:
                    logger.warning("no parsed payload for %s; skipped", filing.accession_no)
                    continue
                event = FilingParsed(
                    accession_no=filing.accession_no,
                    cik=filing.cik,
                    form_type=filing.form_type,
                    parser_version=version,
                )
                enqueue(session, Topic.FILINGS_PARSED, event)
                count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filings-replay", description=__doc__.split("\n")[0])
    parser.add_argument("event", choices=["discovered", "parsed"], help="which event to replay")
    parser.add_argument("--form-type", help="only this EDGAR form type, e.g. 8-K")
    parser.add_argument(
        "--status",
        choices=[s.value for s in FilingStatus],
        action="append",
        help="only filings in this status (repeatable; discovered replays only)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    sessions = session_factory(engine)
    try:
        if args.event == "discovered":
            statuses = [FilingStatus(s) for s in args.status] if args.status else None
            count = replay_discovered(sessions, form_type=args.form_type, statuses=statuses)
        else:
            if args.status:
                parser.error("--status applies to discovered replays only")
            mongo = MongoSettings()
            client: MongoClient[dict[str, Any]] = MongoClient(mongo.mongodb_url.get_secret_value())
            try:
                store = ParsedFilingStore(client[mongo.mongodb_database])
                count = replay_parsed(sessions, store, form_type=args.form_type)
            finally:
                client.close()
    finally:
        engine.dispose()
    logger.info("enqueued %d %s events; the outbox relay publishes them", count, args.event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
