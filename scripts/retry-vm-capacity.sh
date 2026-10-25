#!/usr/bin/env bash
# Get the A1 VM through "Out of host capacity": retry creating it, at either size, rotating the
# region's availability domains; finish what depends on it; then, if it got the smaller size,
# grow it once Oracle has room. Stop at anything unexpected.
#
# Each create attempt plans only the instance (`-target`), checks the plan creates nothing else,
# and applies that plan: nothing else is refreshed or touched, so an attempt is quick and a failed
# one changes nothing. Attempts alternate between the sizes in SIZES, largest first: Oracle often
# has room for 1 OCPU and 6 GB when it has none for 2 and 12 (docs/adr/0020). Once the instance
# exists, two checked plans finish the VM: the (still empty) data volume, replaced in the
# instance's availability domain if it was in another, and its attachment, which the VM's
# bootstrap waits up to 20 minutes for; then Atlas's access-list entry for its IP, which only the
# pipeline needs, so a failure there is reported without stopping the script.
#
# A smaller VM then grows in place, keeping its disks: every GROW_INTERVAL seconds a compute
# capacity report (infra/terraform/tools/capacity-report) asks whether the largest size fits in
# the VM's availability domain, and only when Oracle says AVAILABLE does a checked plan resize it,
# because a resize reboots the VM even when it fails. If the script is started with the instance
# already there, it goes straight to growing.
#
# The script runs `terraform apply` unattended, so start it only after a reviewed first apply
# created everything else (docs/runbook.md, "Oracle idle reclamation and capacity errors").
#
#   scripts/retry-vm-capacity.sh                 # every minute, for up to 24 hours
#   INTERVAL=30 MAX_HOURS=72 SIZES="2:12" scripts/retry-vm-capacity.sh
#
# On success it records the availability domain and size in production.tfvars, so later plans
# keep the VM as it is. Throttling (429) backs off for THROTTLE_WAIT seconds instead of stopping,
# the network failing while planning waits INTERVAL, and a plan lock this machine left behind is
# released. Credentials come from TF_ENV_FILE, like make tf-plan.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_DIR="$ROOT/infra/terraform/envs/production"
CAPACITY_REPORT="$ROOT/infra/terraform/tools/capacity-report"
TF_ENV_FILE=${TF_ENV_FILE:-$HOME/.config/filings/terraform.env}
INTERVAL=${INTERVAL:-60}
THROTTLE_WAIT=${THROTTLE_WAIT:-300}
MAX_HOURS=${MAX_HOURS:-24}
DOMAINS=${DOMAINS:-3}  # Ashburn has three; a region with one should set DOMAINS=1
SIZES=${SIZES:-"2:12 1:6"}  # OCPU:GB, the size to grow into first
GROW_INTERVAL=${GROW_INTERVAL:-300}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

read -r -a sizes <<<"$SIZES"
for size in "${sizes[@]}"; do
  [[ $size =~ ^[0-9]+:[0-9]+$ ]] || { echo "SIZES: '$size' is not OCPU:GB" >&2; exit 1; }
done
target=${sizes[0]}

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

# After a failed plan (output in plan.out): waits out throttling or the network, or releases a
# stale lock, and returns 0 to try again; otherwise prints the error and returns 1.
retry_plan() {
  if grep -qE "$THROTTLED" "$WORK/plan.out"; then
    echo "  throttled while planning; next try in ${THROTTLE_WAIT}s"
    sleep "$THROTTLE_WAIT"
  elif grep -qE "$NETWORK" "$WORK/plan.out"; then
    echo "  network trouble while planning; next try in ${INTERVAL}s"
    sleep "$INTERVAL"
  elif release_stale_lock; then
    echo "  released the stale lock of an earlier plan here; trying again"
  else
    echo "plan failed:"
    grep -A6 'Error' "$WORK/plan.out" | head -20
    return 1
  fi
}

# The plan checks: a create attempt may create the instance and nothing else ("instance"),
# finishing may add only what depends on it ("finish"), and growing may only update the
# instance in place ("grow"). Anything more stops the script.
check_plan() {
  python3 -c '
import json, sys
mode, instance = sys.argv[1], "module.oci.oci_core_instance.k3s"
problems, summary = [], []
for change in json.load(sys.stdin)["resource_changes"]:
    actions, address = change["change"]["actions"], change["address"]
    if actions in (["no-op"], ["read"]):
        continue
    summary.append("/".join(actions) + " " + address)
    if mode == "instance":
        expected = actions == ["create"] and address == instance
    elif mode == "grow":
        expected = actions == ["update"] and address == instance
    else:
        expected = (
            actions == ["create"]
            and address.startswith(
                (
                    "module.oci.oci_core_volume_attachment.data",
                    "module.atlas.mongodbatlas_project_ip_access_list.",
                )
            )
        ) or (address == "module.oci.oci_core_volume.data" and sorted(actions) == ["create", "delete"])
    if not expected:
        problems.append("/".join(actions) + " " + address)
print("  plan:", "; ".join(summary) or "nothing to do")
if problems:
    print("  unexpected:", "; ".join(problems))
    sys.exit(1)
' "$1"
}

# Sets NAME = VALUE in production.tfvars, replacing its line (commented out or not) or adding one.
set_tfvar() {
  local line="$1 = $2 # $3"
  if grep -qE "^#? *$1 *=" production.tfvars; then
    sed -i -E "s|^#? *$1 *=.*|$line|" production.tfvars
  else
    printf '%s\n' "$line" >>production.tfvars
  fi
}

# A string variable from production.tfvars, or nothing when it isn't set there.
tfvar() {
  sed -nE "s/^$1 *= *\"([^\"]*)\".*/\1/p" production.tfvars
}

# Plans (only TARGET and what it depends on), checks, and applies one finishing step.
finish_step() {
  if ! terraform plan -input=false -no-color -var-file=production.tfvars -target="$1" \
    -out=finish.tfplan >"$WORK/plan.out" 2>&1; then
    echo "  plan failed:"
    grep -A6 'Error' "$WORK/plan.out" | head -20
    return 1
  fi
  if ! terraform show -json finish.tfplan | check_plan finish; then
    echo "  stopped: the plan does more than expected; review it with make tf-plan"
    rm -f finish.tfplan
    return 1
  fi
  if ! terraform apply -input=false -no-color finish.tfplan >"$WORK/apply.out" 2>&1; then
    rm -f finish.tfplan
    echo "  apply failed:"
    grep -A8 'Error' "$WORK/apply.out" | head -24
    return 1
  fi
  rm -f finish.tfplan
}

# Finishes the VM once the instance exists: first what it needs to start k3s, its data volume
# (in its availability domain) and the attachment, then Atlas's access-list entry for its IP,
# which only the pipeline needs. Atlas's API refuses Terraform from an address not on the
# service account's API access list, such as a laptop whose home IP changed; the VM runs anyway.
finish() {
  echo "$(date -u +%FT%TZ) finishing: data volume and attachment"
  if ! finish_step module.oci.oci_core_volume_attachment.data; then
    echo "the VM waits 20 minutes for its volume; fix this and run make tf-plan and make tf-apply"
    return 1
  fi
  echo "$(date -u +%FT%TZ) finishing: Atlas access list"
  if ! finish_step module.atlas.mongodbatlas_project_ip_access_list.allowed; then
    echo "  MongoDB refuses the VM until this is applied: if Atlas refused this machine's IP, add"
    echo "  it to the service account's API access list, then run make tf-plan and make tf-apply"
  fi
  echo "$(date -u +%FT%TZ) finished"
}

# Creates the instance, trying each availability domain at each size in turn, then finishes it.
create() {
  local attempt=0 domain size
  while [ "$(date +%s)" -lt "$deadline" ]; do
    size=${sizes[attempt % ${#sizes[@]}]}
    domain=$((attempt / ${#sizes[@]} % DOMAINS + 1))
    attempt=$((attempt + 1))
    echo "$(date -u +%FT%TZ) attempt $attempt, availability domain $domain, ${size%:*} OCPU ${size#*:} GB"
    if ! terraform plan -input=false -no-color -var-file=production.tfvars -target="$INSTANCE" \
      -var "oci_availability_domain_number=$domain" -var "oci_instance_ocpus=${size%:*}" \
      -var "oci_instance_memory_gbs=${size#*:}" -out=retry.tfplan >"$WORK/plan.out" 2>&1; then
      retry_plan || exit 1
      continue
    fi
    if ! terraform show -json retry.tfplan | check_plan instance; then
      echo "stopped: the plan does more than create the instance"
      rm -f retry.tfplan
      exit 1
    fi
    if terraform apply -input=false -no-color retry.tfplan >"$WORK/apply.out" 2>&1; then
      rm -f retry.tfplan
      set_tfvar oci_availability_domain_number "$domain" "the VM got capacity here ($(date -u +%F))"
      set_tfvar oci_instance_ocpus "${size%:*}" "its size when created ($(date -u +%F))"
      set_tfvar oci_instance_memory_gbs "${size#*:}" "its size when created ($(date -u +%F))"
      echo "$(date -u +%FT%TZ) created in availability domain $domain, ${size%:*} OCPU ${size#*:} GB, after $attempt attempt(s)"
      finish || exit 2
      return 0
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
}

# Grows the instance to the target size once a capacity report says the size is available in
# its availability domain.
grow() {
  local shown ad compartment ocpus gbs status profile
  shown=$(terraform state show -no-color "$INSTANCE") || { echo "can't read the instance from the state"; exit 1; }
  ad=$(sed -nE 's/^ *availability_domain *= *"(.*)"$/\1/p' <<<"$shown" | head -1)
  compartment=$(sed -nE 's/^ *compartment_id *= *"(.*)"$/\1/p' <<<"$shown" | head -1)
  # Numbers may show quoted or with decimals ("6", 6.0).
  ocpus=$(sed -nE 's/^ *ocpus *= *"?([0-9]+)(\.[0-9]+)?"?$/\1/p' <<<"$shown" | head -1)
  gbs=$(sed -nE 's/^ *memory_in_gbs *= *"?([0-9]+)(\.[0-9]+)?"?$/\1/p' <<<"$shown" | head -1)
  if [ "$ocpus:$gbs" = "$target" ]; then
    echo "$(date -u +%FT%TZ) the VM has $ocpus OCPU $gbs GB: nothing to grow"
    return 0
  fi
  profile=$(tfvar oci_config_file_profile)
  mkdir -p "$WORK/capacity"
  cp "$CAPACITY_REPORT/main.tf" "$CAPACITY_REPORT/.terraform.lock.hcl" "$WORK/capacity/"
  # The provider production already downloaded (make tf-init).
  if ! terraform -chdir="$WORK/capacity" init -input=false -no-color -lockfile=readonly \
    -plugin-dir="$ENV_DIR/.terraform/providers" >"$WORK/init.out" 2>&1; then
    echo "capacity report init failed:"
    tail -20 "$WORK/init.out"
    exit 1
  fi
  echo "$(date -u +%FT%TZ) the VM has $ocpus OCPU $gbs GB; growing it to ${target%:*} OCPU ${target#*:} GB in $ad when Oracle has room"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    rm -f "$WORK/capacity/terraform.tfstate"
    if ! terraform -chdir="$WORK/capacity" apply -auto-approve -input=false -no-color \
      -var "region=$(tfvar oci_region)" -var "config_file_profile=${profile:-DEFAULT}" \
      -var "compartment_ocid=$compartment" -var "availability_domain=$ad" \
      -var "ocpus=${target%:*}" -var "memory_gbs=${target#*:}" >"$WORK/plan.out" 2>&1; then
      retry_plan || exit 1
      continue
    fi
    status=$(terraform -chdir="$WORK/capacity" output -raw availability)
    if [ "$status" != AVAILABLE ]; then
      echo "$(date -u +%FT%TZ) ${target%:*} OCPU ${target#*:} GB: $status; next check in ${GROW_INTERVAL}s"
      sleep "$GROW_INTERVAL"
      continue
    fi
    echo "$(date -u +%FT%TZ) ${target%:*} OCPU ${target#*:} GB: AVAILABLE; resizing (the VM reboots)"
    if ! terraform plan -input=false -no-color -var-file=production.tfvars -target="$INSTANCE" \
      -var "oci_instance_ocpus=${target%:*}" -var "oci_instance_memory_gbs=${target#*:}" \
      -out=grow.tfplan >"$WORK/plan.out" 2>&1; then
      retry_plan || exit 1
      continue
    fi
    if ! terraform show -json grow.tfplan | check_plan grow; then
      echo "stopped: the plan does more than resize the instance"
      rm -f grow.tfplan
      exit 1
    fi
    if terraform apply -input=false -no-color grow.tfplan >"$WORK/apply.out" 2>&1; then
      rm -f grow.tfplan
      set_tfvar oci_instance_ocpus "${target%:*}" "grown ($(date -u +%F))"
      set_tfvar oci_instance_memory_gbs "${target#*:}" "grown ($(date -u +%F))"
      echo "$(date -u +%FT%TZ) grew the VM to ${target%:*} OCPU ${target#*:} GB. Now remove the small-vm"
      echo "  component from deploy/k8s/overlays/production (docs/adr/0020)."
      return 0
    fi
    rm -f grow.tfplan
    # Someone else took the room between the report and the resize; the VM keeps its size.
    if grep -q 'Out of host capacity' "$WORK/apply.out"; then
      echo "  out of host capacity after all; next check in ${GROW_INTERVAL}s"
      sleep "$GROW_INTERVAL"
      continue
    fi
    echo "resize failed, not for capacity (check that the VM is running):"
    grep -A8 'Error' "$WORK/apply.out" | head -24
    exit 1
  done
  echo "gave up after $MAX_HOURS hours"
  exit 3
}

deadline=$(($(date +%s) + MAX_HOURS * 3600))
if ! terraform state list -no-color >"$WORK/state.out" 2>&1; then
  echo "can't read the state:"
  tail -20 "$WORK/state.out"
  exit 1
fi
if ! grep -qx "$INSTANCE" "$WORK/state.out"; then
  create
fi
grow
