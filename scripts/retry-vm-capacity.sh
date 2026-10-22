#!/usr/bin/env bash
# Retry creating the A1 VM until Oracle has capacity ("Out of host capacity"), rotating the
# region's availability domains, and stop at the first success or anything unexpected.
#
# Each attempt plans, checks the plan, and applies only that plan. The check allows nothing but
# finishing the VM: creating the instance, its volume attachment, and Atlas's access-list entry
# for its IP, plus replacing the (still empty) data volume when the availability domain changes,
# since the volume must be in the instance's domain. It runs `terraform apply` unattended, so
# start it only after a reviewed first apply created everything else (docs/runbook.md, "Oracle
# idle reclamation and capacity errors").
#
#   scripts/retry-vm-capacity.sh                 # every 5 minutes, for up to 24 hours
#   INTERVAL=600 MAX_HOURS=48 DOMAINS=3 scripts/retry-vm-capacity.sh
#
# On success it records the availability domain in production.tfvars, so later plans keep the VM
# where it is. Credentials come from TF_ENV_FILE, like make tf-plan.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_DIR="$ROOT/infra/terraform/envs/production"
TF_ENV_FILE=${TF_ENV_FILE:-$HOME/.config/filings/terraform.env}
INTERVAL=${INTERVAL:-300}
MAX_HOURS=${MAX_HOURS:-24}
DOMAINS=${DOMAINS:-3}  # Ashburn has three; a region with one should set DOMAINS=1
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

set -a
# shellcheck disable=SC1090
. "$TF_ENV_FILE"
set +a
cd "$ENV_DIR" || exit 1

# The plan check: anything beyond finishing the VM stops the loop.
check_plan() {
  python3 -c '
import json, sys
allowed_create = (
    "module.oci.oci_core_instance.k3s",
    "module.oci.oci_core_volume_attachment.data",
    "module.atlas.mongodbatlas_project_ip_access_list.",
)
problems, summary = [], []
for change in json.load(sys.stdin)["resource_changes"]:
    actions, address = change["change"]["actions"], change["address"]
    if actions in (["no-op"], ["read"]):
        continue
    summary.append("/".join(actions) + " " + address)
    if actions == ["create"] and address.startswith(allowed_create):
        continue
    if address == "module.oci.oci_core_volume.data" and sorted(actions) == ["create", "delete"]:
        continue
    problems.append("/".join(actions) + " " + address)
print("  plan:", "; ".join(summary) or "nothing to do")
if problems:
    print("  unexpected:", "; ".join(problems))
    sys.exit(1)
'
}

deadline=$(($(date +%s) + MAX_HOURS * 3600))
attempt=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  domain=$((attempt % DOMAINS + 1))
  attempt=$((attempt + 1))
  echo "$(date -u +%FT%TZ) attempt $attempt, availability domain $domain"
  if ! terraform plan -input=false -var-file=production.tfvars \
    -var "oci_availability_domain_number=$domain" -out=retry.tfplan >"$WORK/plan.out" 2>&1; then
    echo "plan failed:"
    grep -A6 'Error' "$WORK/plan.out" | head -20
    exit 1
  fi
  if ! terraform show -json retry.tfplan | check_plan; then
    echo "stopped: the plan does more than finish the VM"
    rm -f retry.tfplan
    exit 1
  fi
  if terraform apply -input=false retry.tfplan >"$WORK/apply.out" 2>&1; then
    rm -f retry.tfplan
    sed -i "s|^# oci_availability_domain_number = 1 .*|oci_availability_domain_number = $domain # the VM got capacity here ($(date -u +%F))|" production.tfvars
    echo "$(date -u +%FT%TZ) created in availability domain $domain after $attempt attempt(s)"
    exit 0
  fi
  rm -f retry.tfplan
  if grep -q 'Out of host capacity' "$WORK/apply.out"; then
    echo "  out of host capacity; next try in ${INTERVAL}s"
    sleep "$INTERVAL"
    continue
  fi
  echo "apply failed, not for capacity:"
  grep -A8 'Error' "$WORK/apply.out" | head -24
  exit 1
done
echo "gave up after $MAX_HOURS hours"
exit 3
