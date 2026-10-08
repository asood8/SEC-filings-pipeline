"""Query embeddings cached in Redis, so a repeated search doesn't wait on the embedding model.

A query's vector depends only on the model and the text, so one cached vector serves every mode,
filter, and page of that query, which the response cache (keyed by the whole URL) can't. Like the
response cache it fails open: on a Redis error the query is embedded as if nothing were cached,
and Redis is skipped for a while (the shared `RedisBackoff`).
"""

import hashlib
from array import array

import redis

from filings_api.middleware import RedisBackoff
from filings_common.db import EMBEDDING_DIMENSIONS

CACHE_PREFIX = "query-embedding:v1"
# Kept short because Valkey refuses writes when full (maxmemory without an eviction policy), so
# what it holds must stay small: about 3 KB per distinct query.
TTL_SECONDS = 600
VECTOR_BYTES = array("f").itemsize * EMBEDDING_DIMENSIONS


def cache_key(model: str, query: str) -> str:
    return f"{CACHE_PREFIX}:{model}:{hashlib.sha256(query.encode()).hexdigest()}"


class QueryEmbeddingCache:
    """Vectors are stored as 32-bit floats, the precision pgvector keeps anyway."""

    def __init__(
        self, client: redis.Redis, backoff: RedisBackoff, *, ttl_seconds: int = TTL_SECONDS
    ) -> None:
        self._client = client
        self._backoff = backoff
        self._ttl = ttl_seconds

    def get(self, model: str, query: str) -> list[float] | None:
        if not self._backoff.available():
            return None
        try:
            cached = self._client.get(cache_key(model, query))
        except redis.RedisError:
            self._backoff.failed()
            return None
        if not isinstance(cached, bytes) or len(cached) != VECTOR_BYTES:
            return None
        vector = array("f")
        vector.frombytes(cached)
        return vector.tolist()

    def put(self, model: str, query: str, vector: list[float]) -> None:
        if not self._backoff.available():
            return
        try:
            self._client.set(cache_key(model, query), array("f", vector).tobytes(), ex=self._ttl)
        except redis.RedisError:
            self._backoff.failed()
