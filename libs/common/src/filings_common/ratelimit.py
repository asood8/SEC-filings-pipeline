"""Token-bucket rate limiting shared by every worker through Redis.

Per-process throttles don't coordinate, so five workers each limited to 5 req/s would send 25.
Instead, one bucket lives in Redis and a Lua script updates it atomically, using Redis's clock so
all workers agree on time.
"""

import time
from collections.abc import Callable
from typing import Protocol

import redis

# Reservation-style bucket: every call takes a token immediately, letting the balance go
# negative, and returns how long the caller must wait before using it. Callers are served in
# arrival order and nobody polls.
# KEYS[1] = bucket key; ARGV[1] = refill rate (tokens/second); ARGV[2] = capacity (burst).
_TOKEN_BUCKET_LUA = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or burst
local ts = tonumber(state[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate) - 1
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
-- Once the bucket would be full again its state carries no information, so let it expire.
redis.call('PEXPIRE', KEYS[1], math.ceil((burst - tokens) / rate * 1000) + 1000)
if tokens >= 0 then
  return '0'
end
return tostring(-tokens / rate)
"""


class RateLimiter(Protocol):
    def acquire(self) -> float:
        """Block until one request may be made. Returns the seconds spent waiting."""
        ...


class RedisTokenBucket:
    def __init__(
        self,
        client: redis.Redis,
        key: str,
        rate_per_second: float,
        burst: int = 1,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate_per_second must be positive and burst at least 1")
        self._script = client.register_script(_TOKEN_BUCKET_LUA)
        self._key = key
        self._rate = rate_per_second
        self._burst = burst
        self._sleep = sleep

    def reserve(self) -> float:
        """Take a token and return how many seconds to wait before using it."""
        reply = self._script(keys=[self._key], args=[self._rate, self._burst])
        return float(reply.decode() if isinstance(reply, bytes) else reply)

    def acquire(self) -> float:
        wait = self.reserve()
        if wait > 0:
            self._sleep(wait)
        return wait


# Classic bucket for request admission: a token is taken only if one is available; otherwise the
# reply says how long until one will be. Nothing waits server-side.
_ADMISSION_LUA = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or burst
local ts = tonumber(state[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  retry = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil((burst - tokens) / rate * 1000) + 1000)
return tostring(retry)
"""


class RedisRateLimit:
    """Per-key admission control (for example per API client), shared through Redis.

    `check(key)` takes a token when one is available and returns 0; otherwise it returns the
    seconds until the next token, and the caller should reject the request (HTTP 429).
    """

    def __init__(
        self, client: redis.Redis, prefix: str, rate_per_second: float, burst: int
    ) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate_per_second must be positive and burst at least 1")
        self._script = client.register_script(_ADMISSION_LUA)
        self._prefix = prefix
        self._rate = rate_per_second
        self._burst = burst

    def check(self, key: str) -> float:
        reply = self._script(keys=[f"{self._prefix}:{key}"], args=[self._rate, self._burst])
        return float(reply.decode() if isinstance(reply, bytes) else reply)
