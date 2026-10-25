#!/usr/bin/env bash
# Retry creating the A1 VM until Oracle has capacity ("Out of host capacity"), rotating the
# region's availability domains, then finish what depends on it; stop at anything unexpected.
#
# Each attempt plans only the instance (`-target`), checks the plan creates nothing else, and
# applies that plan: nothing else is refreshed or touched, so an attempt is quick and a failed
# one changes nothing. Once the instance exists, one more checked plan finishes the VM: its
# volume attachment, Atlas's access-list entry for its IP, and the (still empty) data volume
# replaced in the instance's availability domain if it was in another. The VM's bootstrap waits
# up to 20 minutes for the volume. The script runs `terraform apply` unattended, so start it only
# after a reviewed first apply created everything else (docs/runbook.md, "Oracle idle
# reclamation and capacity errors").
#
#   scripts/retry-vm-capacity.sh                 # every minute, for up to 24 hours
#   INTERVAL=300 MAX_HOURS=48 DOMAINS=3 scripts/retry-vm-capacity.sh
#
# On success it records the availability domain in production.tfvars, so later plans keep the VM
# where it is. Throttling (429) backs off for THROTTLE_WAIT seconds instead of stopping, the
# network failing while planning waits INTERVAL, and a plan lock this machine left behind is
# released. Credentials come from TF_ENV_FILE, like make tf-plan.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_DIR="$ROOT/infra/terraform/envs/production"
TF_ENV_FILE=${TF_ENV_FILE:-$HOME/.config/filings/terraform.env}
INTERVAL=${INTERVAL:-60}
THROTTLE_WAIT=${THROTTLE_WAIT:-300}
MAX_HOURS=${MAX_HOURS:-24}
DOMAINS=${DOMAINS:-3}  # Ashburn has three; a region with one should set DOMAINS=1
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

set -a
# shellcheck disable=SC1090
. "$TF_ENV_FILE"
set +a
cd "$ENV_DIR" || exit 1

INSTANCE=module.oci.oci_core_instance.k3s
# Throttling, from Oracle or from R2 (where the state lives): wait THROTTLE_WAIT and try again.
THROTTLED='TooManyRequests|Status(Code)?: 429|Reduce your rate'
# The network failing (Wi-Fi dropping, the laptop waking up) while planning: a plan changes
# nothing, so wait INTERVAL and try again. Not while applying, where Oracle may have created an
# instance Terraform never heard back about: that still stops the script.
NETWORK='dial tcp|i/o timeout|no such host|connection reset by peer|TLS handshake timeout'

# A plan cut off (by the network, or by stopping the script) can leave its state lock behind,
# and every later plan fails on it. A plan's lock taken by this user on this machine, with no
# terraform running here, is stale, and plans don't write state, so releasing it is safe. Any
# other lock stops the script.
release_stale_lock() {
  local id
  if ! grep -q 'Error acquiring the state lock' "$WORK/plan.out" ||
    ! grep -qE '^ *Operation: +OperationTypePlan$' "$WORK/plan.out" ||
    ! grep -qE "^ *Who: +$(whoami)@$(hostname)$" "$WORK/plan.out" ||
    pgrep -x terraform >/dev/null; then
    return 1
  fi
  id=$(sed -nE 's/^ *ID: +([0-9a-f-]+)$/\1/p' "$WORK/plan.out" | head -1)
  [ -n "$id" ] && terraform force-unlock -force "$id" >/dev/null
}

# The plan checks: an attempt may create the instance and nothing else ("instance"); finishing
# may add only what depends on it ("finish"). Anything more stops the script.
check_plan() {
  python3 -c '
import json, sys
if sys.argv[1] == "instance":
    allowed_create, volume_replace = ("module.oci.oci_core_instance.k3s",), False
else:
    allowed_create, volume_replace = (
        "module.oci.oci_core_volume_attachment.data",
        "module.atlas.mongodbatlas_project_ip_access_list.",
    ), True
problems, summary = [], []
for change in json.load(sys.stdin)["resource_changes"]:
    actions, address = change["change"]["actions"], change["address"]
    if actions in (["no-op"], ["read"]):
        continue
    summary.append("/".join(actions) + " " + address)
    if actions == ["create"] and address.startswith(allowed_create):
        continue
    if (
        volume_replace
        and address == "module.oci.oci_core_volume.data"
        and sorted(actions) == ["create", "delete"]
    ):
        continue
    problems.append("/".join(actions) + " " + address)
print("  plan:", "; ".join(summary) or "nothing to do")
if problems:
    print("  unexpected:", "; ".join(problems))
    sys.exit(1)
' "$1"
}

# Plans, checks, and applies the rest once the instance exists.
finish() {
  echo "$(date -u +%FT%TZ) finishing: volume, attachment, Atlas access list"
  if ! terraform plan -input=false -var-file=production.tfvars -out=finish.tfplan \
    >"$WORK/plan.out" 2>&1; then
    echo "plan failed:"
    grep -A6 'Error' "$WORK/plan.out" | head -20
    return 1
  fi
  if ! terraform show -json finish.tfplan | check_plan finish; then
    echo "stopped: the finishing plan does more than expected; review it with make tf-plan"
    rm -f finish.tfplan
    return 1
  fi
  if ! terraform apply -input=false finish.tfplan >"$WORK/apply.out" 2>&1; then
    rm -f finish.tfplan
    echo "finishing apply failed (the VM waits 20 minutes for its volume; rerun make tf-apply):"
    grep -A8 'Error' "$WORK/apply.out" | head -24
    return 1
  fi
  rm -f finish.tfplan
  echo "$(date -u +%FT%TZ) finished"
}

deadline=$(($(date +%s) + MAX_HOURS * 3600))
attempt=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  domain=$((attempt % DOMAINS + 1))
  attempt=$((attempt + 1))
  echo "$(date -u +%FT%TZ) attempt $attempt, availability domain $domain"
  if ! terraform plan -input=false -no-color -var-file=production.tfvars -target="$INSTANCE" \
    -var "oci_availability_domain_number=$domain" -out=retry.tfplan >"$WORK/plan.out" 2>&1; then
    if grep -qE "$THROTTLED" "$WORK/plan.out"; then
      echo "  throttled while planning; next try in ${THROTTLE_WAIT}s"
      sleep "$THROTTLE_WAIT"
      continue
    fi
    if grep -qE "$NETWORK" "$WORK/plan.out"; then
      echo "  network trouble while planning; next try in ${INTERVAL}s"
      sleep "$INTERVAL"
      continue
    fi
    if release_stale_lock; then
      echo "  released the stale lock of an earlier plan here; trying again"
      continue
    fi
    echo "plan failed:"
    grep -A6 'Error' "$WORK/plan.out" | head -20
    exit 1
  fi
  if ! terraform show -json retry.tfplan | check_plan instance; then
    echo "stopped: the plan does more than create the instance"
    rm -f retry.tfplan
    exit 1
  fi
  if terraform apply -input=false retry.tfplan >"$WORK/apply.out" 2>&1; then
    rm -f retry.tfplan
    sed -i "s|^# oci_availability_domain_number = 1 .*|oci_availability_domain_number = $domain # the VM got capacity here ($(date -u +%F))|" production.tfvars
    echo "$(date -u +%FT%TZ) created in availability domain $domain after $attempt attempt(s)"
    finish || exit 2
    exit 0
  fi
  rm -f retry.tfplan
  if grep -q 'Out of host capacity' "$WORK/apply.out"; then
    echo "  out of host capacity; next try in ${INTERVAL}s"
    sleep "$INTERVAL"
    continue
  fi
  if grep -qE "$THROTTLED" "$WORK/apply.out"; then
    echo "  throttled; next try in ${THROTTLE_WAIT}s"
    sleep "$THROTTLE_WAIT"
    continue
  fi
  echo "apply failed, not for capacity:"
  grep -A8 'Error' "$WORK/apply.out" | head -24
  exit 1
done
echo "gave up after $MAX_HOURS hours"
exit 3
