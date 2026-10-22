"""The API: filings, companies, insider trades, and search (public), plus Google sign-in and
alert subscriptions. Docs at /docs."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import redis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy.orm import Session, sessionmaker

from filings_api.embedding_cache import QueryEmbeddingCache
from filings_api.google import GoogleIdTokenVerifier, GoogleSigningKeys
from filings_api.middleware import (
    PROBE_PATHS,
    CacheMiddleware,
    CsrfMiddleware,
    RateLimitMiddleware,
    RedisBackoff,
    SecurityHeadersMiddleware,
)
from filings_api.readiness import DatabaseReadiness
from filings_api.routes import (
    auth,
    calls,
    companies,
    filings,
    insider_trades,
    search,
    subscriptions,
)
from filings_common.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GoogleSignInSettings,
    RedisSettings,
)
from filings_common.db import create_engine, session_factory
from filings_common.embeddings import Embedder, embedder_from_settings
from filings_common.ratelimit import RedisRateLimit
from filings_common.telemetry import Telemetry, setup_telemetry

logger = logging.getLogger(__name__)

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


def instrument(app: FastAPI) -> None:
    """Trace each request with a SERVER span, and measure it, except health checks (probes would
    flood traces).

    The span records the method, route, URL, and status, never request headers or bodies, which
    carry the session cookie, Google ID tokens, and webhook URLs. Query strings hold only search
    terms and filters. Should header capture ever be turned on (OTEL_INSTRUMENTATION_HTTP_CAPTURE_
    HEADERS_*), cookie and authorization values are still redacted.

    Metrics: `http.server.request.duration` by method, route, and status, and
    `http.server.active_requests`. They follow the stable HTTP semantic conventions, which the
    instrumentation only uses when asked: its older default has no route, and labels requests by
    their Host header, which any client can set to anything.
    """
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")  # read when first instrumenting
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=",".join(sorted(PROBE_PATHS)),
        exclude_spans=["receive", "send"],
        http_capture_headers_sanitize_fields=["cookie", "authorization"],
    )


def create_app(
    sessions: sessionmaker[Session] | None = None,
    *,
    redis_client: redis.Redis | None = None,
    embedder: Embedder | None = None,
    settings: ApiSettings | None = None,
    google_verifier: GoogleIdTokenVerifier | None = None,
) -> FastAPI:
    """Build the app. Given `sessions` (tests), it uses exactly the dependencies passed in;
    otherwise it connects to everything from the environment on startup, and sets up logging and
    tracing for the process."""
    settings = settings or ApiSettings()
    telemetry = setup_telemetry("filings-api") if sessions is None else Telemetry()
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per embedding call is noise

    def configure(
        app: FastAPI,
        client: redis.Redis | None,
        emb: Embedder | None,
        verifier: GoogleIdTokenVerifier | None,
    ) -> None:
        app.state.embedder = emb
        app.state.google_verifier = verifier
        app.state.cache = client
        app.state.query_embeddings = (
            QueryEmbeddingCache(client, app.state.redis_backoff)
            if client is not None and settings.cache_ttl_seconds > 0
            else None
        )
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
            configure(app, redis_client, embedder, google_verifier)
            yield
            return
        engine = create_engine(
            DatabaseSettings().database_url.get_secret_value(), hide_parameters=True
        )
        client = api_redis_client(RedisSettings().redis_url)
        google_keys = GoogleSigningKeys()
        client_id = GoogleSignInSettings().client_id
        if client_id is None:
            logger.warning("GOOGLE_CLIENT_ID is not set: sign-in is disabled")
        app.state.sessions = session_factory(engine)
        configure(
            app,
            client,
            embedder_from_settings(EmbeddingSettings()),
            GoogleIdTokenVerifier(client_id, google_keys) if client_id else None,
        )
        yield
        google_keys.close()
        client.close()
        engine.dispose()
        telemetry.shutdown()

    app = FastAPI(
        title="SEC Filings API", version="0.2.0", description=DESCRIPTION, lifespan=lifespan
    )
    app.state.sessions = None
    app.state.embedder = None
    app.state.google_verifier = None
    app.state.cache = None
    app.state.query_embeddings = None
    app.state.rate_limit = None
    app.state.redis_backoff = RedisBackoff()
    app.state.readiness = DatabaseReadiness()
    app.state.cache_ttl_seconds = settings.cache_ttl_seconds
    app.state.trust_proxy_headers = settings.trust_proxy_headers
    app.state.session_lifetime = timedelta(days=settings.session_lifetime_days)
    app.state.cookie_secure = settings.cookie_secure
    app.state.allowed_origins = frozenset(settings.cors_origins)
    # Starlette runs the last-added middleware first, so requests pass through these bottom-up:
    # CORS, then the rate limit, the CSRF check, and the cache.
    app.add_middleware(CacheMiddleware)
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(RateLimitMiddleware)
    # CORS goes outermost, deliberately:
    # - It answers preflight requests itself, so they don't spend the client's rate limit.
    # - Its headers reach every response, including 429s and 403s; without them the website
    #   couldn't read those errors.
    # - Its headers depend on the request's Origin and are added outside the cache, so a cached
    #   response never carries another site's Access-Control-Allow-Origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,  # exact origins only; the settings reject "*"
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    # Outside everything, so even CORS preflights and cached responses carry them.
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness: the process answers. Nothing else, so a database outage never gets the API
        restarted (that is what /readyz is for)."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: whether this replica can serve, which takes Postgres. While it fails,
        Kubernetes sends the replica no traffic, and a rollout waits for it."""
        readiness: DatabaseReadiness = request.app.state.readiness
        if await readiness.problem(request.app.state.sessions) is not None:
            # Only the logs say why: /readyz is reachable from outside.
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    app.include_router(filings.router)
    app.include_router(companies.router)
    app.include_router(insider_trades.router)
    app.include_router(calls.router)
    app.include_router(search.router)
    app.include_router(auth.router)
    app.include_router(subscriptions.router)
    if telemetry.enabled:
        instrument(app)
    return app
