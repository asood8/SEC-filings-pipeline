"""Daily look at what exists in the Oracle account (`filings-oci-inventory`; ADR-0018).

Oracle bills a Pay As You Go account for whatever exceeds Always Free and has no spending cap.
Its budget alert is evaluated once a day on cost data that is itself a day old, so this job
doesn't wait for a cost: once a day it lists what exists, which is known at once, and compares
it with what the project creates. With a key that can only read, it checks that:

- every resource is of an expected kind, within the expected number (one VM, its boot volume
  and one data volume, one network): anything else, or one too many, is "unexpected";
- the tenancy is subscribed to its home region only (Always Free applies there alone);
- the guardrails are in place: the quota policy, active, with its statements, and the budget
  with both alert rules.

It records three gauges for the alert rules in deploy/grafana: `filings.oci.resources.unexpected`,
`filings.oci.guardrails.missing`, and `filings.oci.budget.spend` (what Oracle says was spent
this month). The log names kinds and counts, never an identifier.
"""

import argparse
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2

from filings_common import metrics
from filings_common.config import OciInventorySettings
from filings_common.oci import OciClient, OciError
from filings_common.telemetry import setup_telemetry

logger = logging.getLogger(__name__)

# How many of each kind may exist (Resource Search's resourceType). What Terraform creates
# (infra/terraform/modules/oci), what Oracle creates with a tenancy or a VM, and identity
# records, which cost nothing at any number (None).
EXPECTED: dict[str, int | None] = {
    # The VM and its disks: the Always Free allowance, as the quota policy caps it.
    "Instance": 1,
    "BootVolume": 1,
    "Volume": 1,
    "VolumeAttachment": 1,
    "BootVolumeAttachment": 1,
    "Vnic": 1,
    "VnicAttachment": 1,
    "PrivateIp": 1,
    "PublicIp": 1,
    "ConsoleHistory": 5,
    # One network.
    "Vcn": 1,
    "Subnet": 1,
    "InternetGateway": 1,
    "RouteTable": 2,
    "SecurityList": 2,
    "DHCPOptions": 1,
    "DnsResolver": 1,
    "DnsView": 1,
    "CustomerDnsZone": 2,
    # The guardrails.
    "Quota": 1,
    "Budget": 1,
    # Identity: free.
    "App": None,
    "Compartment": None,
    "Group": None,
    "Policy": None,
    "User": None,
    "TagDefault": None,
    "TagNamespace": None,
}
GONE = frozenset({"TERMINATED", "DELETED"})  # Search lists these for a while afterwards
# Statements the quota policy can't lack (the whole list is in modules/oci/guardrails.tf).
REQUIRED_STATEMENTS = (
    "zero compute-core quotas in tenancy",
    "zero compute-memory quotas in tenancy",
    "zero block-storage quotas in tenancy",
    "zero database quotas in tenancy",
)


@dataclass
class Inventory:
    unexpected: dict[str, int] = field(default_factory=dict)  # kind -> how many too many
    guardrails: list[str] = field(default_factory=list)  # what is missing or weakened
    spend: float | None = None  # this month, in the account's currency
    counts: dict[str, int] = field(default_factory=dict)


def unexpected_resources(items: Sequence[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    """(how many of each kind exist, how many of each are beyond what is expected)."""
    counts = Counter(
        item["resourceType"] for item in items if item.get("lifecycleState") not in GONE
    )
    over = {}
    for kind, count in counts.items():
        allowed = EXPECTED.get(kind, 0)
        if allowed is not None and count > allowed:
            over[kind] = count - allowed
    return dict(sorted(counts.items())), dict(sorted(over.items()))


def check(client: OciClient, settings: OciInventorySettings) -> Inventory:
    inventory = Inventory()
    inventory.counts, inventory.unexpected = unexpected_resources(
        client.search("query all resources")
    )

    regions = client.get("identity", f"/20160918/tenancies/{client.tenancy}/regionSubscriptions")
    if len(regions) != 1:
        names = ", ".join(sorted(r["regionName"] for r in regions))
        inventory.guardrails.append(f"subscribed to {len(regions)} regions ({names})")

    quotas = client.get("limits", "/20181025/quotas", compartmentId=client.tenancy)
    quota = next((q for q in quotas if q["name"] == settings.quota_name), None)
    if quota is None or quota.get("lifecycleState") != "ACTIVE":
        inventory.guardrails.append(f"quota policy {settings.quota_name} is missing or inactive")
    else:
        statements = client.get("limits", f"/20181025/quotas/{quota['id']}")["statements"]
        lacking = [s for s in REQUIRED_STATEMENTS if s not in statements]
        if lacking or len(statements) < settings.min_quota_statements:
            inventory.guardrails.append(
                f"quota policy {settings.quota_name} was weakened: {len(statements)} statements"
                f" (at least {settings.min_quota_statements} expected), {len(lacking)} of the"
                " required ones missing"
            )

    budgets = client.get("usage", "/20190111/budgets", compartmentId=client.tenancy)
    budget = next((b for b in budgets if b["displayName"] == settings.budget_name), None)
    if budget is None or budget.get("lifecycleState") != "ACTIVE":
        inventory.guardrails.append(f"budget {settings.budget_name} is missing or inactive")
    else:
        inventory.spend = float(budget.get("actualSpend") or 0)
        if budget.get("alertRuleCount", 0) < 2 or float(budget["amount"]) > settings.max_budget:
            inventory.guardrails.append(
                f"budget {settings.budget_name} was weakened: {budget.get('alertRuleCount', 0)}"
                f" alert rules, amount {budget['amount']}"
            )
    return inventory


def report(inventory: Inventory) -> None:
    """Record the gauges and log what was found."""
    metrics.OCI_UNEXPECTED_VALUES.replace(report, [(sum(inventory.unexpected.values()), {})])
    metrics.OCI_GUARDRAILS_VALUES.replace(report, [(len(inventory.guardrails), {})])
    metrics.OCI_SPEND_VALUES.replace(
        report, [] if inventory.spend is None else [(inventory.spend, {})]
    )
    listed = ", ".join(f"{count} {kind}" for kind, count in inventory.counts.items())
    logger.info("the Oracle account holds: %s", listed or "nothing")
    for kind, extra in inventory.unexpected.items():
        logger.warning("unexpected in the Oracle account: %d %s more than expected", extra, kind)
    for problem in inventory.guardrails:
        logger.warning("guardrail: %s", problem)
    if inventory.spend:
        logger.warning(
            "Oracle reports %.2f spent this month; everything should be free", inventory.spend
        )
    if not inventory.unexpected and not inventory.guardrails and not inventory.spend:
        logger.info("as expected: nothing extra, the guardrails in place, nothing spent")


def run(settings: OciInventorySettings, *, transport: httpx2.BaseTransport | None = None) -> int:
    try:
        with OciClient(
            tenancy=settings.tenancy_ocid,
            user=settings.user_ocid,
            fingerprint=settings.fingerprint,
            private_key_pem=settings.private_key.get_secret_value().encode(),
            region=settings.region,
            transport=transport,
        ) as client:
            inventory = check(client, settings)
    except (OciError, KeyError, ValueError) as exc:
        logger.error("couldn't read the Oracle account: %r", exc)
        return 1
    report(inventory)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="filings-oci-inventory", description=(__doc__ or "").split("\n")[0]
    ).parse_args(argv)
    telemetry = setup_telemetry("filings-oci-inventory", distribution="filings-common")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    try:
        return run(OciInventorySettings())
    finally:
        telemetry.shutdown()  # a job exits right after: export the gauges first
