"""Per-client rate limiting and response caching, both shared through Redis.

Both fail open: if Redis is unreachable, requests are served uncached and unlimited rather than
failing. After an error, Redis is skipped for a few seconds, so an outage costs one timeout rather
than one per request.
"""

import logging
import math
import time
from collections.abc import Callable
from urllib.parse import urlencode

import redis
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from filings_common.ratelimit import RedisRateLimit

logger = logging.getLogger(__name__)

UNLIMITED_PATHS = frozenset({"/healthz"})
UNCACHED_PATHS = frozenset({"/healthz", "/openapi.json", "/docs", "/redoc"})
CACHE_PREFIX = "api-cache:v1"


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


class CacheMiddleware(BaseHTTPMiddleware):
    """Caches successful JSON GET responses for a short time. Data changes slowly (new filings
    arrive minutes apart), so a short TTL needs no invalidation."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cache: redis.Redis | None = request.app.state.cache
        ttl: int = request.app.state.cache_ttl_seconds
        backoff: RedisBackoff = request.app.state.redis_backoff
        if (
            cache is None
            or ttl <= 0
            or request.method != "GET"
            or request.url.path in UNCACHED_PATHS
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
