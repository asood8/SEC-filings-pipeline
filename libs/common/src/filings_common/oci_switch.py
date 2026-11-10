"""The Oracle account's off switch (`filings-oci-vm`, `make oci-status|oci-stop|oci-off`).

For the day Oracle's budget email or the inventory alert says something costs money (runbook,
"Guardrails against charges"). It uses the operator's own API key (~/.oci/config), and acts on
every VM and volume in the account, whoever created them:

- `status`: what exists that could be billed, and what the budget says was spent;
- `stop`: stops every VM. Their disks and data stay; processor and memory stop counting;
- `off`: terminates every VM and deletes every volume, after asking to type a phrase. The
  production database goes with them (the nightly backups in R2 don't); the network, the quota
  policy and the budget stay, and cost nothing.

Terraform still lists what `off` deleted: the next `make tf-plan` shows them as to be created.
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx2

from filings_common.oci import OciClient, OciError

logger = logging.getLogger(__name__)

CONFIRMATION = "delete everything"
GONE = frozenset({"TERMINATED", "DELETED"})
WAIT_SECONDS = 600  # for the VMs to finish terminating, before their volumes can go
POLL_SECONDS = 10


def _live(client: OciClient, kind: str) -> list[dict[str, Any]]:
    found = client.search(f"query {kind} resources")
    return [item for item in found if item.get("lifecycleState") not in GONE]


def _act(client: OciClient, method: str, path: str, **params: str) -> None:
    response = client.request(method, "iaas", path, params=params or None)
    if response.status_code not in (200, 202, 204, 404):  # 404: already gone
        raise OciError(f"{method} {path.rsplit('/', 2)[-2]}: HTTP {response.status_code}")


def status(client: OciClient) -> int:
    billable = 0
    for kind in ("instance", "bootvolume", "volume"):
        items = _live(client, kind)
        billable += len(items)
        states = ", ".join(sorted(str(i.get("lifecycleState")) for i in items)) or "none"
        print(f"{kind}: {len(items)} ({states})")
    for budget in client.get("usage", "/20190111/budgets", compartmentId=client.tenancy):
        print(
            f"budget {budget['displayName']}: {budget.get('actualSpend') or 0} spent this month,"
            f" {budget.get('forecastedSpend') or 0} forecast"
        )
    return 0


def stop(client: OciClient) -> int:
    instances = _live(client, "instance")
    for instance in instances:
        if instance.get("lifecycleState") not in ("STOPPED", "STOPPING"):
            _act(client, "POST", f"/20160918/instances/{instance['identifier']}", action="STOP")
    print(f"stopping {len(instances)} VM(s); their disks and data are kept")
    return 0


def off(
    client: OciClient,
    *,
    confirm: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    confirm, sleep = confirm or input, sleep or time.sleep
    instances = _live(client, "instance")
    volumes = _live(client, "volume")
    print(
        f"This terminates {len(instances)} VM(s) and deletes their boot volumes and"
        f" {len(volumes)} data volume(s). The production database is on them."
    )
    if confirm(f'Type "{CONFIRMATION}" to go on: ').strip() != CONFIRMATION:
        print("nothing was changed")
        return 1
    for instance in instances:
        _act(
            client,
            "DELETE",
            f"/20160918/instances/{instance['identifier']}",
            preserveBootVolume="false",
        )
    # A volume can't be deleted while its VM still holds it.
    waited = 0.0
    while instances and waited < WAIT_SECONDS:
        states = {
            client.get("iaas", f"/20160918/instances/{i['identifier']}")["lifecycleState"]
            for i in instances
        }
        if states <= GONE:
            break
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    else:
        if instances:
            print("the VMs are still terminating; run this again in a few minutes for the volumes")
            return 1
    for volume in volumes:
        _act(client, "DELETE", f"/20160918/volumes/{volume['identifier']}")
    for boot in _live(client, "bootvolume"):  # one kept from an earlier VM
        _act(client, "DELETE", f"/20160918/bootVolumes/{boot['identifier']}")
    print(f"terminated {len(instances)} VM(s) and deleted the volumes")
    return 0


def main(
    argv: Sequence[str] | None = None, *, transport: httpx2.BaseTransport | None = None
) -> int:
    parser = argparse.ArgumentParser(
        prog="filings-oci-vm", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("action", choices=["status", "stop", "off"])
    args = parser.parse_args(argv)
    actions = {"status": status, "stop": stop, "off": off}
    try:
        with OciClient.from_config_file(transport=transport) as client:
            return actions[args.action](client)
    except OciError as exc:
        print(f"Oracle's API: {exc}", file=sys.stderr)
        return 1
