"""Per-client rate limiting and response caching, both shared through Redis, and a CSRF check.

Rate limiting and caching fail open: if Redis is unreachable, requests are served uncached and
unlimited rather than failing. After an error, Redis is skipped for a few seconds, so an outage
costs one timeout rather than one per request.
"""

import logging
import math
import time
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit

import redis
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from filings_api.auth import SESSION_COOKIE_NAMES
from filings_common.ratelimit import RedisRateLimit

logger = logging.getLogger(__name__)

# The Kubernetes probes: never rate limited (they come from the node, often) or cached (they must
# see the current state). Not traced either (`app.instrument`).
PROBE_PATHS = frozenset({"/healthz", "/readyz"})
UNLIMITED_PATHS = PROBE_PATHS
UNCACHED_PATHS = PROBE_PATHS | {"/openapi.json", "/docs", "/redoc"}
# Per-user responses: never cached here, and marked no-store so browsers and proxies don't either.
PRIVATE_PATH_PREFIXES = ("/auth", "/me", "/subscriptions")
CACHE_PREFIX = "api-cache:v1"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RedisBackoff:
    """Shared by both middlewares: after a Redis error, stop calling Redis for `seconds`."""

    def __init__(self, seconds: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._until = 0.0

    def available(self) -> bool:
        return self._clock() >= self._until

    def failed(self) -> None:
        logger.warning(
            "Redis unavailable; serving without cache and rate limits for %.0fs", self._seconds
        )
        self._until = self._clock() + self._seconds


def client_id(request: Request, *, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers and (forwarded := request.headers.get("CF-Connecting-IP")):
        return forwarded
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        limiter: RedisRateLimit | None = request.app.state.rate_limit
        backoff: RedisBackoff = request.app.state.redis_backoff
        if limiter is None or request.url.path in UNLIMITED_PATHS or not backoff.available():
            return await call_next(request)
        client = client_id(request, trust_proxy_headers=request.app.state.trust_proxy_headers)
        try:
            retry_after = limiter.check(client)
        except redis.RedisError:
            backoff.failed()
            retry_after = 0.0
        if retry_after > 0:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(math.ceil(retry_after))},
            )
        return await call_next(request)


def cache_key(request: Request) -> str:
    query = urlencode(sorted(request.query_params.multi_items()))
    return f"{CACHE_PREFIX}:{request.url.path}?{query}"


def is_private_path(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in PRIVATE_PATH_PREFIXES)


def _is_per_user(request: Request) -> bool:
    has_session = any(name in request.cookies for name in SESSION_COOKIE_NAMES)
    return has_session or "authorization" in request.headers


def _forbids_storing(response: Response) -> bool:
    directives = response.headers.get("cache-control", "").lower().split(",")
    return any(d.split("=", 1)[0].strip() in {"private", "no-store"} for d in directives)


class CacheMiddleware(BaseHTTPMiddleware):
    """Caches successful JSON GET responses for a short time. Data changes slowly (new filings
    arrive minutes apart), so a short TTL needs no invalidation.

    The cache key is only the URL, so anything that could differ between users stays out of the
    cache: private paths, requests with a session cookie or an Authorization header, and
    responses that set a cookie or say Cache-Control private or no-store.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if is_private_path(request.url.path):
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        cache: redis.Redis | None = request.app.state.cache
        ttl: int = request.app.state.cache_ttl_seconds
        backoff: RedisBackoff = request.app.state.redis_backoff
        if (
            cache is None
            or ttl <= 0
            or request.method != "GET"
            or request.url.path in UNCACHED_PATHS
            or _is_per_user(request)
            or not backoff.available()
        ):
            return await call_next(request)
        key = cache_key(request)
        try:
            cached = cache.get(key)
        except redis.RedisError:
            backoff.failed()
            return await call_next(request)
        if cached is not None:
            return Response(
                content=cached, media_type="application/json", headers={"X-Cache": "HIT"}
            )

        response = await call_next(request)
        if (
            response.status_code != 200
            or response.headers.get("content-type") != "application/json"
            or "set-cookie" in response.headers
            or _forbids_storing(response)
        ):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        try:
            cache.set(key, body, ex=ttl)
        except redis.RedisError:
            backoff.failed()
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        headers["X-Cache"] = "MISS"
        return Response(
            content=body, status_code=200, headers=headers, media_type="application/json"
        )


def origin_allowed(request: Request, origin: str) -> bool:
    """The website's origins, and the API's own (for its /docs page)."""
    allowed: frozenset[str] = request.app.state.allowed_origins
    host = request.headers.get("host", "").lower()
    return origin in allowed or (bool(host) and urlsplit(origin).netloc.lower() == host)


def _has_body(request: Request) -> bool:
    length = request.headers.get("content-length", "").strip()
    return "transfer-encoding" in request.headers or length not in {"", "0"}


def _is_json(content_type: str | None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


class CsrfMiddleware(BaseHTTPMiddleware):
    """Defense in depth against cross-site request forgery, on top of the SameSite=Lax cookie.

    - A state-changing request whose Origin header names another site is refused (403). Browsers
      send Origin with every cross-origin POST, PUT, PATCH, and DELETE; other clients (curl,
      scripts) usually send none, and aren't affected.
    - Request bodies must be JSON (415 otherwise). A page on another site can send form or plain
      text bodies without asking, but cross-origin JSON needs a CORS preflight first, which only
      the configured origins pass.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in STATE_CHANGING_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not origin_allowed(request, origin):
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
            if _has_body(request) and not _is_json(request.headers.get("content-type")):
                return JSONResponse({"detail": "request body must be JSON"}, status_code=415)
        return await call_next(request)
