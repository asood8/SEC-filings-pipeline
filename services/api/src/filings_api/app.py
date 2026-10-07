"""The public read API: filings, companies, and search. Docs at /docs."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy.orm import Session, sessionmaker

from filings_api.middleware import CacheMiddleware, RateLimitMiddleware, RedisBackoff
from filings_api.routes import companies, filings, search
from filings_common.config import ApiSettings, DatabaseSettings, EmbeddingSettings, RedisSettings
from filings_common.db import create_engine, session_factory
from filings_common.embeddings import Embedder, embedder_from_settings
from filings_common.ratelimit import RedisRateLimit

RATE_LIMIT_PREFIX = "ratelimit:api"

DESCRIPTION = """
Recent SEC filings from a watchlist of public companies, with plain-English summaries: Form 4
insider trades and Form 8-K current reports.

Summaries are generated automatically and may contain errors. They are not investment advice;
each filing links to the original on sec.gov.
"""


def api_redis_client(url: str) -> redis.Redis:
    """A Redis client that gives up fast. The API only uses Redis for caching and rate limits,
    so a slow Redis must not make requests slow: short timeouts, and no retries."""
    return redis.Redis.from_url(
        url, socket_connect_timeout=0.5, socket_timeout=0.5, retry=Retry(NoBackoff(), 0)
    )


def create_app(
    sessions: sessionmaker[Session] | None = None,
    *,
    redis_client: redis.Redis | None = None,
    embedder: Embedder | None = None,
    settings: ApiSettings | None = None,
) -> FastAPI:
    """Build the app. Given `sessions` (tests), it uses exactly the dependencies passed in;
    otherwise it connects to everything from the environment on startup."""
    settings = settings or ApiSettings()

    def configure(app: FastAPI, client: redis.Redis | None, emb: Embedder | None) -> None:
        app.state.embedder = emb
        app.state.cache = client
        app.state.rate_limit = (
            RedisRateLimit(
                client,
                RATE_LIMIT_PREFIX,
                settings.rate_limit_per_minute / 60,
                settings.rate_limit_burst,
            )
            if client is not None
            else None
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if sessions is not None:
            app.state.sessions = sessions
            configure(app, redis_client, embedder)
            yield
            return
        engine = create_engine(DatabaseSettings().database_url.get_secret_value())
        client = api_redis_client(RedisSettings().redis_url)
        app.state.sessions = session_factory(engine)
        configure(app, client, embedder_from_settings(EmbeddingSettings()))
        yield
        client.close()
        engine.dispose()

    app = FastAPI(
        title="SEC Filings API", version="0.2.0", description=DESCRIPTION, lifespan=lifespan
    )
    app.state.sessions = None
    app.state.embedder = None
    app.state.cache = None
    app.state.rate_limit = None
    app.state.redis_backoff = RedisBackoff()
    app.state.cache_ttl_seconds = settings.cache_ttl_seconds
    app.state.trust_proxy_headers = settings.trust_proxy_headers
    # Starlette runs the last-added middleware first: rate limiting happens before the cache.
    app.add_middleware(CacheMiddleware)
    app.add_middleware(RateLimitMiddleware)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(filings.router)
    app.include_router(companies.router)
    app.include_router(search.router)
    return app
