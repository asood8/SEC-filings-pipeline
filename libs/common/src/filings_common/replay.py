"""Re-announce filings from Postgres through the outbox: `filings-replay discovered | parsed`.

- `discovered` re-publishes `filings.discovered`, so the parser parses the filings again: after a
  new parser is added or a parser version changes. Parsing is idempotent. A filing that is already
  `parsed` is updated in place but not announced again.
- `parsed` re-publishes `filings.parsed`, so the summarizer summarizes again: after a prompt or
  model change.

Events go through the outbox, so they are published by the relay with the usual at-least-once
guarantee. Consumers are idempotent, so replaying is always safe.

Like discovery, each replayed filing starts a trace of its own (linked to the replay run's span),
which follows it through the stages it goes through again.
"""

import argparse
import logging
from collections.abc import Sequence
from contextlib import AbstractContextManager
from itertools import batched
from typing import Any

import sqlalchemy as sa
from opentelemetry import trace
from opentelemetry.trace import Span
from pymongo import MongoClient
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import DatabaseSettings, MongoSettings
from filings_common.db import Filing, FilingStatus, create_engine, session_factory
from filings_common.events import FilingDiscovered, FilingParsed, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.outbox import enqueue
from filings_common.telemetry import filing_trace, setup_telemetry

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

BATCH_SIZE = 500
# Statuses a filing has once it has been parsed.
PARSED_OR_LATER = (FilingStatus.PARSED, FilingStatus.READY, FilingStatus.NOTIFIED)


def _filings(
    session: Session,
    *,
    form_type: str | None,
    statuses: Sequence[FilingStatus] | None,
    accession_nos: Sequence[str] | None = None,
) -> list[Filing]:
    query = sa.select(Filing).order_by(Filing.filed_at, Filing.accession_no)
    if accession_nos is not None:
        query = query.where(Filing.accession_no.in_(accession_nos))
    if form_type is not None:
        query = query.where(Filing.form_type == form_type)
    if statuses is not None:
        query = query.where(Filing.status.in_(statuses))
    return list(session.scalars(query))


def _filing_trace(filing: Filing, topic: Topic) -> AbstractContextManager[Span]:
    return filing_trace(
        tracer, f"replay {topic}", filing.accession_no, form_type=filing.form_type, cik=filing.cik
    )


def replay_discovered(
    sessions: sessionmaker[Session],
    *,
    form_type: str | None = None,
    statuses: Sequence[FilingStatus] | None = None,
    accession_nos: Sequence[str] | None = None,
) -> int:
    """Enqueue `filings.discovered` for matching filings. Returns how many."""
    with sessions() as session:
        filings = _filings(
            session, form_type=form_type, statuses=statuses, accession_nos=accession_nos
        )
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
                with _filing_trace(filing, Topic.FILINGS_DISCOVERED):
                    enqueue(session, Topic.FILINGS_DISCOVERED, event)
    return len(filings)


def replay_parsed(
    sessions: sessionmaker[Session],
    parsed: ParsedFilingStore,
    *,
    form_type: str | None = None,
    accession_nos: Sequence[str] | None = None,
) -> int:
    """Enqueue `filings.parsed` for parsed filings. Returns how many.

    The parser version comes from the stored payload; filings with no payload are skipped.
    """
    with sessions() as session:
        filings = _filings(
            session, form_type=form_type, statuses=PARSED_OR_LATER, accession_nos=accession_nos
        )
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
                with _filing_trace(filing, Topic.FILINGS_PARSED):
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
    parser.add_argument(
        "--accession",
        action="append",
        help="only this filing, by accession number (repeatable): e.g. to reprocess one filing",
    )
    args = parser.parse_args(argv)
    if args.status and args.event != "discovered":
        parser.error("--status applies to discovered replays only")
    telemetry = setup_telemetry("filings-replay", distribution="filings-common")

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    sessions = session_factory(engine)
    try:
        with tracer.start_as_current_span(f"replay {args.event}"):
            if args.event == "discovered":
                statuses = [FilingStatus(s) for s in args.status] if args.status else None
                count = replay_discovered(
                    sessions,
                    form_type=args.form_type,
                    statuses=statuses,
                    accession_nos=args.accession,
                )
            else:
                count = _replay_parsed(
                    sessions, form_type=args.form_type, accession_nos=args.accession
                )
    finally:
        engine.dispose()
        telemetry.shutdown()
    logger.info("enqueued %d %s events; the outbox relay publishes them", count, args.event)
    return 0


def _replay_parsed(
    sessions: sessionmaker[Session], *, form_type: str | None, accession_nos: list[str] | None
) -> int:
    mongo = MongoSettings()
    client: MongoClient[dict[str, Any]] = MongoClient(mongo.mongodb_url.get_secret_value())
    try:
        return replay_parsed(
            sessions,
            ParsedFilingStore(client[mongo.mongodb_database]),
            form_type=form_type,
            accession_nos=accession_nos,
        )
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
