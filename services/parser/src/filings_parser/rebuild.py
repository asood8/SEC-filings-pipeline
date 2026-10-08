"""Rebuild Postgres key fields from stored payloads: `filings-rebuild-key-fields`.

Run it after changing what a parser writes to Postgres, so filings parsed before get the change
too: for example `filings-rebuild-key-fields --form-type 4` after `form4_transactions` was added.
It reads the parsed payloads from MongoDB, so it fetches nothing (from SEC or object storage),
reparses nothing, and publishes no events. Filing statuses are left alone.

- **Idempotent:** key fields are upserts, and a Form 4's transaction lines are rewritten as a set,
  so running it again leaves exactly the same rows.
- **Which filings:** those parsed or further along whose form has key fields. Each is rewritten in
  its own transaction.
- **Skipped,** with a warning and exit status 1: a filing with no stored payload, or one parsed by
  another parser version. A payload's meaning can change between versions, so reparse those
  instead (`filings-replay discovered --accession ...`), which rewrites their key fields as well.
"""

import argparse
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Any

import sqlalchemy as sa
from opentelemetry import trace
from pydantic import ValidationError
from pymongo import MongoClient
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import DatabaseSettings, MongoSettings
from filings_common.db import Filing, create_engine, session_factory
from filings_common.mongo import ParsedFilingStore
from filings_common.replay import PARSED_OR_LATER
from filings_common.telemetry import setup_telemetry
from filings_parser.forms import PARSERS, FormParser

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

BATCH_SIZE = 200  # payloads fetched from MongoDB at a time


@dataclass
class RebuildResult:
    rebuilt: int = 0
    skipped: int = 0


def forms_with_key_fields(parsers: Mapping[str, FormParser]) -> list[str]:
    return sorted(form for form, parser in parsers.items() if parser.save_key_fields is not None)


def rebuild_key_fields(
    sessions: sessionmaker[Session],
    parsed: ParsedFilingStore,
    parsers: Mapping[str, FormParser] = PARSERS,
    *,
    form_type: str | None = None,
    accession_nos: Sequence[str] | None = None,
) -> RebuildResult:
    """Rewrite the key fields of matching parsed filings from their stored payloads."""
    forms = [f for f in forms_with_key_fields(parsers) if form_type in (None, f)]
    query = (
        sa.select(Filing.accession_no, Filing.form_type)
        .where(Filing.status.in_(PARSED_OR_LATER), Filing.form_type.in_(forms))
        .order_by(Filing.filed_at, Filing.accession_no)
    )
    if accession_nos is not None:
        query = query.where(Filing.accession_no.in_(accession_nos))
    with sessions() as session:
        filings = session.execute(query).all()

    result = RebuildResult()
    for batch in batched(filings, BATCH_SIZE):
        documents = parsed.get_many([accession_no for accession_no, _ in batch])
        for accession_no, form in batch:
            if _rebuild(sessions, parsers[form], accession_no, documents.get(accession_no)):
                result.rebuilt += 1
            else:
                result.skipped += 1
    return result


def _rebuild(
    sessions: sessionmaker[Session],
    parser: FormParser,
    accession_no: str,
    document: dict[str, Any] | None,
) -> bool:
    if parser.save_key_fields is None:
        return False
    if document is None:
        logger.warning("%s has no parsed payload in MongoDB; skipped", accession_no)
        return False
    if document["parser_version"] != parser.version:
        logger.warning(
            "%s was parsed by parser version %s, not %s; skipped (reparse it with "
            "`filings-replay discovered --accession %s`)",
            accession_no,
            document["parser_version"],
            parser.version,
            accession_no,
        )
        return False
    try:
        with sessions.begin() as session:
            parser.save_key_fields(session, accession_no, document["parsed"])
    except ValidationError as exc:
        logger.warning("%s has a payload the parser can't read; skipped: %s", accession_no, exc)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filings-rebuild-key-fields", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "--form-type", choices=forms_with_key_fields(PARSERS), help="only this EDGAR form type"
    )
    parser.add_argument(
        "--accession", action="append", help="only this filing, by accession number (repeatable)"
    )
    args = parser.parse_args(argv)
    telemetry = setup_telemetry("filings-rebuild-key-fields", distribution="filings-parser")

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    mongo = MongoSettings()
    client: MongoClient[dict[str, Any]] = MongoClient(mongo.mongodb_url.get_secret_value())
    try:
        with tracer.start_as_current_span("rebuild key fields"):
            result = rebuild_key_fields(
                session_factory(engine),
                ParsedFilingStore(client[mongo.mongodb_database]),
                form_type=args.form_type,
                accession_nos=args.accession,
            )
    finally:
        client.close()
        engine.dispose()
        telemetry.shutdown()
    logger.info("rebuilt the key fields of %d filings; skipped %d", result.rebuilt, result.skipped)
    return 1 if result.skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
