"""The daily R2 free-tier check, against a stand-in for Cloudflare's GraphQL Analytics API (the
response shape is the one in https://developers.cloudflare.com/r2/platform/metrics-analytics/)."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest

from filings_common.config import R2UsageSettings
from filings_common.r2_usage import FREE_STORAGE_BYTES, Usage, run, usage_from

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
SETTINGS = R2UsageSettings(account_id="fedcba9876543210fedcba9876543210", analytics_token="t0ken")


def operations(**requests: int) -> list[dict[str, Any]]:
    return [{"sum": {"requests": n}, "dimensions": {"actionType": a}} for a, n in requests.items()]


def storage(*rows: tuple[str, str, int]) -> list[dict[str, Any]]:
    """(bucket, datetime, bytes) rows, newest first as the query orders them."""
    return [
        {
            "max": {"payloadSize": size, "metadataSize": 0},
            "dimensions": {"bucketName": b, "datetime": t},
        }
        for b, t, size in rows
    ]


def test_operations_are_counted_by_class_and_deletes_are_free() -> None:
    usage = usage_from(
        {
            "operations": operations(PutObject=300, ListObjects=20, GetObject=900, DeleteObject=50),
            "storage": [],
        }
    )
    assert (usage.class_a, usage.class_b, usage.unknown_actions) == (320, 900, frozenset())


def test_an_operation_missing_from_the_price_list_counts_as_class_a() -> None:
    usage = usage_from({"operations": operations(PutObject=1, DeleteObjects=7), "storage": []})
    assert usage.class_a == 8
    assert usage.unknown_actions == frozenset({"DeleteObjects"})


def test_storage_is_each_buckets_latest_size_added_up() -> None:
    usage = usage_from(
        {
            "operations": [],
            "storage": storage(
                ("filings-raw", "2026-10-22T11:00:00Z", 8_000_000),
                ("filings-backups", "2026-10-22T10:00:00Z", 2_000_000),
                ("filings-raw", "2026-10-21T11:00:00Z", 7_000_000),  # older, ignored
            ),
        }
    )
    assert usage.storage_bytes == 10_000_000


def test_fractions_of_the_free_tier() -> None:
    used = Usage(
        storage_bytes=FREE_STORAGE_BYTES // 4, class_a=500_000, class_b=1_000
    ).free_tier_used()
    assert used == {"storage": 0.25, "class_a": 0.5, "class_b": 0.0001}


class FakeCloudflare:
    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


def analytics(account: dict[str, Any] | None) -> httpx2.Response:
    accounts = [] if account is None else [account]
    return httpx2.Response(200, json={"data": {"viewer": {"accounts": accounts}}, "errors": None})


def test_run_records_the_gauge_and_warns_past_half(
    measured: Any, caplog: pytest.LogCaptureFixture
) -> None:
    cloudflare = FakeCloudflare(
        analytics(
            {
                "operations": operations(PutObject=600_000, GetObject=10_000),
                "storage": storage(("filings-raw", "2026-10-22T11:00:00Z", 7_500_000)),
            }
        )
    )
    with caplog.at_level(logging.INFO, logger="filings_common.r2_usage"):
        assert run(SETTINGS, transport=httpx2.MockTransport(cloudflare), now=NOW) == 0

    assert measured.gauge("filings.r2.free_tier.used") == {
        "class_a": 0.6,
        "class_b": 0.001,
        "storage": 0.00075,
    }
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "R2 class_a: 60.00% of the free tier (600,000 of 1,000,000 operations in 30 days)"
    ]

    [request] = cloudflare.requests
    assert request.headers["Authorization"] == "Bearer t0ken"
    variables = json.loads(request.content)["variables"]
    assert variables == {
        "accountTag": "fedcba9876543210fedcba9876543210",
        "operationsSince": "2026-09-22T12:00:00Z",
        "storageSince": "2026-10-20T12:00:00Z",
        "until": "2026-10-22T12:00:00Z",
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(403, json={"errors": [{"message": "Authentication error"}]}),
        httpx2.Response(200, json={"data": None, "errors": [{"message": "unknown field"}]}),
        analytics(None),  # a token without access to this account sees no accounts
        httpx2.Response(200, text="<html>maintenance</html>"),
    ],
)
def test_failures_fail_the_run(response: httpx2.Response, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="filings_common.r2_usage"):
        assert run(SETTINGS, transport=httpx2.MockTransport(FakeCloudflare(response)), now=NOW) == 1
    assert [r.getMessage()[:26] for r in caplog.records] == ["couldn't read R2's usage: "]
