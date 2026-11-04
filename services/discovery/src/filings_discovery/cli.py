"""Command line entry point: `filings-discovery load-companies | run | fetch-exhibits |
fetch-transcripts`."""

import argparse
import logging
import signal
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

import redis
from opentelemetry import trace
from pymongo import MongoClient
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import (
    DatabaseSettings,
    EdgarSettings,
    MongoSettings,
    ObjectStoreSettings,
    RedisSettings,
)
from filings_common.db import create_engine, session_factory
from filings_common.edgar import EdgarClient
from filings_common.mongo import TranscriptStore
from filings_common.storage import object_store_from_settings
from filings_common.telemetry import setup_telemetry
from filings_discovery.companies import (
    UnknownTickersError,
    parse_company_tickers_exchange,
    resolve_watchlist,
    sync_companies,
)
from filings_discovery.config import DiscoverySettings
from filings_discovery.job import backfill_exhibits, discover
from filings_discovery.transcripts import SiteClient, fetch_transcripts, load_sources
from filings_discovery.watchlist import WatchlistError, load_watchlist

logger = logging.getLogger("filings_discovery")
tracer = trace.get_tracer(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filings-discovery")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("load-companies", help="load SEC's ticker map and flag the watchlist")
    commands.add_parser("run", help="find, store, and announce new watchlist filings")
    commands.add_parser(
        "fetch-exhibits", help="download EX-99 exhibits for known filings that have none"
    )
    commands.add_parser(
        "fetch-transcripts",
        help="store new earnings-call transcripts from the companies' own sites (no SEC calls)",
    )
    args = parser.parse_args(argv)
    telemetry = setup_telemetry("filings-discovery")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per request is too noisy
    try:
        signal.signal(signal.SIGTERM, exit_on_sigterm)
        return _run(args.command)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a late SIGTERM can't cut the export short
        telemetry.shutdown()  # a job exits right after: export its spans first


def exit_on_sigterm(signum: int, frame: FrameType | None) -> None:
    """Stop at once, but through the `finally` blocks: the filing in progress is rolled back
    (as when killed) and the spans so far are exported.

    Kubernetes sends SIGTERM when a run outlives its deadline, and that run's trace is the one
    worth reading. Without a handler, Python running as PID 1 ignores SIGTERM and is killed at
    the end of the grace period, its buffered spans lost.
    """
    logger.warning("stopping: received SIGTERM")
    raise SystemExit(128 + signum)


def _run(command: str) -> int:
    settings = DiscoverySettings()
    try:
        tickers = load_watchlist(settings.watchlist_path)
    except WatchlistError as exc:
        logger.error("%s", exc)
        return 2

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    sessions = session_factory(engine)
    if command == "fetch-transcripts":
        try:
            return _fetch_transcripts(settings, sessions)
        finally:
            engine.dispose()
    redis_client = redis.Redis.from_url(RedisSettings().redis_url)
    try:
        with EdgarClient.from_settings(EdgarSettings(), redis_client) as edgar:
            if command == "load-companies":
                with tracer.start_as_current_span("load companies"):
                    records = parse_company_tickers_exchange(edgar.company_tickers_exchange())
                    try:
                        ciks = resolve_watchlist(records, tickers)
                    except UnknownTickersError as exc:
                        logger.error("%s", exc)
                        return 2
                    with sessions.begin() as session:
                        count = sync_companies(session, records, ciks)
                logger.info("loaded %d companies, %d on the watchlist", count, len(ciks))
                return 0

            store = object_store_from_settings(ObjectStoreSettings())
            if command == "fetch-exhibits":
                with tracer.start_as_current_span("fetch exhibits"):
                    checked, stored = backfill_exhibits(
                        sessions, edgar, store, form_types=settings.exhibit_form_types
                    )
                logger.info("checked %d filings, stored %d exhibits", checked, stored)
                return 0

            since = datetime.now(UTC).date() - timedelta(days=settings.lookback_days)
            result = discover(
                sessions,
                edgar,
                store,
                form_types=settings.form_types,
                since=since,
                exhibit_form_types=settings.exhibit_form_types,
            )
            logger.info(
                "polled %d companies: %d new filings, %d failed",
                result.companies,
                result.new_filings,
                len(result.failed_ciks),
            )
            return 1 if result.failed_ciks else 0
    finally:
        redis_client.close()
        engine.dispose()


def _fetch_transcripts(settings: DiscoverySettings, sessions: sessionmaker[Session]) -> int:
    try:
        sources = load_sources(settings.transcript_sources_path)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    mongo_settings = MongoSettings()
    mongo: MongoClient[dict[str, Any]] = MongoClient(mongo_settings.mongodb_url.get_secret_value())
    try:
        with (
            tracer.start_as_current_span("fetch transcripts"),
            SiteClient(settings.transcript_user_agent) as client,
        ):
            report = fetch_transcripts(
                sessions,
                TranscriptStore(mongo[mongo_settings.mongodb_database]),
                client,
                sources,
                today=datetime.now(UTC).date(),
                lookback_days=settings.transcript_lookback_days,
                overdue_days=settings.transcript_overdue_days,
            )
    finally:
        mongo.close()
    logger.info(
        "checked %d transcript sources: %d new transcripts, %d failed, %d overdue",
        len(sources),
        len(report.stored),
        len(report.failed),
        len(report.overdue),
    )
    return 1 if report.failed else 0
