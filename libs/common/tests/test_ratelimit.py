import itertools
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from filings_common.ratelimit import RedisRateLimit, RedisTokenBucket

pytestmark = pytest.mark.integration


def new_key() -> str:
    return f"test:ratelimit:{uuid.uuid4().hex}"


def test_burst_is_free_then_requests_are_spaced_by_the_rate(redis_client: redis.Redis) -> None:
    bucket = RedisTokenBucket(redis_client, new_key(), rate_per_second=10, burst=3)
    calls = []
    for _ in range(6):
        called_at = time.monotonic()
        calls.append((called_at, bucket.reserve()))
    assert [wait for _, wait in calls[:3]] == [0, 0, 0]
    # Each reservation's slot (call time + wait) is 1/rate after the previous one, however long
    # the calls themselves take, so this holds on a busy machine too.
    slots = [called_at + wait for called_at, wait in calls[2:]]
    for earlier, later in itertools.pairwise(slots):
        assert later - earlier == pytest.approx(0.1, abs=0.02)


def test_workers_with_separate_limiters_share_one_bucket(redis_client: redis.Redis) -> None:
    key = new_key()
    workers = [RedisTokenBucket(redis_client, key, rate_per_second=20) for _ in range(4)]

    def make_three_requests(limiter: RedisTokenBucket) -> None:
        for _ in range(3):
            limiter.acquire()

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(make_three_requests, workers))
    elapsed = time.monotonic() - start
    # 12 requests at 20/s with no burst: the first is free, the rest are 50 ms apart.
    assert 0.5 <= elapsed < 1.5


def test_bucket_refills_while_idle_and_then_expires(redis_client: redis.Redis) -> None:
    key = new_key()
    bucket = RedisTokenBucket(redis_client, key, rate_per_second=20, burst=2)
    assert [bucket.reserve(), bucket.reserve()] == [0, 0]
    assert bucket.reserve() > 0
    time.sleep(0.2)
    assert bucket.reserve() == 0
    assert 0 < redis_client.pttl(key) <= 2000


def test_rejects_nonsense_configuration(redis_client: redis.Redis) -> None:
    with pytest.raises(ValueError, match="rate_per_second"):
        RedisTokenBucket(redis_client, new_key(), rate_per_second=0)


def test_admission_allows_the_burst_then_rejects_with_retry_time(redis_client: redis.Redis) -> None:
    limit = RedisRateLimit(redis_client, new_key(), rate_per_second=10, burst=3)
    assert [limit.check("client-a") for _ in range(3)] == [0, 0, 0]
    retry = limit.check("client-a")
    assert 0 < retry <= 0.1
    assert limit.check("client-b") == 0  # each client has its own bucket
    time.sleep(retry + 0.02)
    assert limit.check("client-a") == 0
