"""Daily check of R2's free tier (`filings-r2-usage`): how much of each monthly allowance is used.

R2 has no spending cap: past the free tier, Cloudflare bills the payment method on file. This job
reads Cloudflare's own usage numbers once a day, through the GraphQL Analytics API with a token
that can only read analytics, and records the fraction of each allowance used as the
`filings.r2.free_tier.used` gauge. An alert fires past half of any of them
(deploy/grafana/alerting/filings.yaml), long before anything could be billed.

The allowances, per month (https://developers.cloudflare.com/r2/pricing/, checked 2026-10-22):
10 GB-month of Standard storage, 1 million Class A operations (writes and lists), and 10 million
Class B operations (reads); deletes are free. They are measured on the safe side:

- storage is the buckets' current size, and a month's average can't pass the allowance while
  the current size stays below it;
- operations are counted over the last 30 days, which covers any billing month to date;
- an operation type missing from the price list counts as Class A, the smaller allowance.
"""

import argparse
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2

from filings_common import metrics
from filings_common.config import R2UsageSettings
from filings_common.telemetry import setup_telemetry

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

FREE_STORAGE_BYTES = 10 * 10**9  # 10 GB, read as 10^9 bytes: the smaller reading
FREE_CLASS_A = 1_000_000
FREE_CLASS_B = 10_000_000
WARN_AT = 0.5  # the alert rule's threshold too

OPERATIONS_WINDOW = timedelta(days=30)  # the API allows at most 31 days per query
STORAGE_WINDOW = timedelta(days=2)  # long enough to include each bucket's latest measurement

# Cloudflare's price list, by S3 operation name.
CLASS_A = frozenset(
    [
        "ListBuckets",
        "PutBucket",
        "ListObjects",
        "PutObject",
        "CopyObject",
        "CompleteMultipartUpload",
        "CreateMultipartUpload",
        "LifecycleStorageTierTransition",
        "ListMultipartUploads",
        "UploadPart",
        "UploadPartCopy",
        "ListParts",
        "PutBucketEncryption",
        "PutBucketCors",
        "PutBucketLifecycleConfiguration",
    ]
)
CLASS_B = frozenset(
    [
        "HeadBucket",
        "HeadObject",
        "GetObject",
        "UsageSummary",
        "GetBucketEncryption",
        "GetBucketLocation",
        "GetBucketCors",
        "GetBucketLifecycleConfiguration",
    ]
)
FREE = frozenset(["DeleteObject", "DeleteBucket", "AbortMultipartUpload"])

QUERY = """
query R2Usage($accountTag: string!, $operationsSince: Time!, $storageSince: Time!, $until: Time!) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      operations: r2OperationsAdaptiveGroups(
        limit: 10000
        filter: { datetime_geq: $operationsSince, datetime_leq: $until }
      ) {
        sum { requests }
        dimensions { actionType }
      }
      storage: r2StorageAdaptiveGroups(
        limit: 10000
        filter: { datetime_geq: $storageSince, datetime_leq: $until }
        orderBy: [datetime_DESC]
      ) {
        max { payloadSize metadataSize }
        dimensions { bucketName datetime }
      }
    }
  }
}
"""


class R2UsageError(Exception):
    """Cloudflare's analytics couldn't be read."""


@dataclass(frozen=True)
class Usage:
    storage_bytes: int
    class_a: int
    class_b: int
    # Operation types counted as Class A because the price list doesn't name them.
    unknown_actions: frozenset[str] = field(default=frozenset())

    def free_tier_used(self) -> dict[str, float]:
        """The fraction of each allowance used, by `filings.r2.measure`."""
        return {
            "storage": self.storage_bytes / FREE_STORAGE_BYTES,
            "class_a": self.class_a / FREE_CLASS_A,
            "class_b": self.class_b / FREE_CLASS_B,
        }


def usage_from(account: Mapping[str, Any]) -> Usage:
    """Usage from one account of the analytics response (see QUERY)."""
    class_a = class_b = 0
    unknown: set[str] = set()
    for group in account["operations"]:
        action, requests = group["dimensions"]["actionType"], int(group["sum"]["requests"])
        if action in FREE:
            continue
        if action in CLASS_B:
            class_b += requests
        else:
            class_a += requests
            if action not in CLASS_A:
                unknown.add(action)
    latest: dict[str, int] = {}
    for group in account["storage"]:  # newest first: keep each bucket's latest size
        bucket = group["dimensions"]["bucketName"]
        if bucket not in latest:
            sizes = group["max"]
            latest[bucket] = int(sizes["payloadSize"]) + int(sizes["metadataSize"])
    return Usage(sum(latest.values()), class_a, class_b, frozenset(unknown))


def fetch_usage(client: httpx2.Client, account_id: str, now: datetime) -> Usage:
    def iso(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    variables = {
        "accountTag": account_id,
        "operationsSince": iso(now - OPERATIONS_WINDOW),
        "storageSince": iso(now - STORAGE_WINDOW),
        "until": iso(now),
    }
    response = client.post(GRAPHQL_URL, json={"query": QUERY, "variables": variables})
    if not response.is_success:
        raise R2UsageError(f"Cloudflare's analytics API answered {response.status_code}")
    body = response.json()
    if body.get("errors"):
        raise R2UsageError("; ".join(str(e.get("message", e)) for e in body["errors"]))
    accounts = body["data"]["viewer"]["accounts"]
    if not accounts:
        raise R2UsageError("the token can't read this account's analytics")
    return usage_from(accounts[0])


def report(usage: Usage) -> dict[str, float]:
    """Record the gauge and log each allowance's use; a warning past WARN_AT."""
    used = usage.free_tier_used()
    metrics.R2_FREE_TIER_VALUES.replace(
        report, [(fraction, {metrics.R2_MEASURE: measure}) for measure, fraction in used.items()]
    )
    details = {
        "storage": f"{usage.storage_bytes / 10**9:.3f} GB of 10",
        "class_a": f"{usage.class_a:,} of {FREE_CLASS_A:,} operations in 30 days",
        "class_b": f"{usage.class_b:,} of {FREE_CLASS_B:,} operations in 30 days",
    }
    for measure, fraction in used.items():
        log = logger.warning if fraction > WARN_AT else logger.info
        log("R2 %s: %.2f%% of the free tier (%s)", measure, 100 * fraction, details[measure])
    if usage.unknown_actions:
        logger.warning(
            "counted as Class A, not in the price list: %s",
            ", ".join(sorted(usage.unknown_actions)),
        )
    return used


def run(
    settings: R2UsageSettings,
    *,
    transport: httpx2.BaseTransport | None = None,
    now: datetime | None = None,
) -> int:
    headers = {"Authorization": f"Bearer {settings.analytics_token.get_secret_value()}"}
    with httpx2.Client(headers=headers, timeout=30.0, transport=transport) as client:
        try:
            usage = fetch_usage(client, settings.account_id, now or datetime.now(UTC))
        except (R2UsageError, httpx2.TransportError, KeyError, ValueError) as exc:
            logger.error("couldn't read R2's usage: %r", exc)
            return 1
    report(usage)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(prog="filings-r2-usage", description=__doc__.split("\n")[0]).parse_args(
        argv
    )
    telemetry = setup_telemetry("filings-r2-usage", distribution="filings-common")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    try:
        return run(R2UsageSettings())
    finally:
        telemetry.shutdown()  # a job exits right after: export the gauge first
