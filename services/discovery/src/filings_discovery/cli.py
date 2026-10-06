"""Command line entry point: `filings-discovery load-companies | run`."""

import argparse
import logging
from datetime import UTC, datetime, timedelta

import redis

from filings_common.config import (
    DatabaseSettings,
    EdgarSettings,
    ObjectStoreSettings,
    RedisSettings,
)
from filings_common.db import create_engine, session_factory
from filings_common.edgar import EdgarClient
from filings_common.storage import object_store_from_settings
from filings_discovery.companies import (
    UnknownTickersError,
    parse_company_tickers_exchange,
    resolve_watchlist,
    sync_companies,
)
from filings_discovery.config import DiscoverySettings
from filings_discovery.job import discover
from filings_discovery.watchlist import WatchlistError, load_watchlist

logger = logging.getLogger("filings_discovery")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filings-discovery")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("load-companies", help="load SEC's ticker map and flag the watchlist")
    commands.add_parser("run", help="find, store, and announce new watchlist filings")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per request is too noisy

    settings = DiscoverySettings()
    try:
        tickers = load_watchlist(settings.watchlist_path)
    except WatchlistError as exc:
        logger.error("%s", exc)
        return 2

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    sessions = session_factory(engine)
    redis_client = redis.Redis.from_url(RedisSettings().redis_url)
    try:
        with EdgarClient.from_settings(EdgarSettings(), redis_client) as edgar:
            if args.command == "load-companies":
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

            since = datetime.now(UTC).date() - timedelta(days=settings.lookback_days)
            store = object_store_from_settings(ObjectStoreSettings())
            result = discover(sessions, edgar, store, form_types=settings.form_types, since=since)
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
