#!/usr/bin/env sh
# Free-tier guardrail for the Terraform code (run by `make tf-check` and CI).
#
# Fails if any .tf file declares a resource type that isn't on the allowlist below, or uses a
# module from outside this directory (which could create resources this check can't see). Each
# allowed type was checked against the free tiers in docs/PROJECT_BRIEF.md §9 (October 2026), and
# the modules' variable validations keep its settings inside them. Adding a type here is a
# decision: confirm it is free first, and record why in docs/adr/0018-terraform.md.
set -eu

cd "$(dirname "$0")"

allowed='
oci_core_vcn
oci_core_default_security_list
oci_core_internet_gateway
oci_core_route_table
oci_core_security_list
oci_core_subnet
oci_core_instance
oci_core_volume
oci_core_volume_attachment
cloudflare_zero_trust_tunnel_cloudflared
cloudflare_zero_trust_tunnel_cloudflared_config
cloudflare_dns_record
cloudflare_r2_bucket
cloudflare_r2_bucket_lifecycle
cloudflare_pages_project
cloudflare_pages_domain
mongodbatlas_project
mongodbatlas_advanced_cluster
mongodbatlas_database_user
mongodbatlas_project_ip_access_list
grafana_folder
grafana_dashboard
grafana_rule_group
random_password
'

tf_files=$(find . -name .terraform -prune -o -name '*.tf' -print)

status=0

used=$(echo "$tf_files" | xargs grep -hoE '^resource "[a-z0-9_]+"' | cut -d'"' -f2 | sort -u)
for type in $used; do
  if ! echo "$allowed" | grep -qx "$type"; then
    echo "check-free-tier: resource type '$type' is not on the free-tier allowlist" >&2
    status=1
  fi
done

# Module sources must be local paths ("./" or "../").
remote=$(echo "$tf_files" | xargs grep -nE '^[[:space:]]*source[[:space:]]*=[[:space:]]*"' |
  grep -vE 'source[[:space:]]*=[[:space:]]*"\.\.?/' |
  grep -vE '"[a-z0-9-]+/[a-z0-9-]+"$' || true)
# (The last filter skips provider sources such as "oracle/oci" in required_providers blocks.)
if [ -n "$remote" ]; then
  echo "check-free-tier: modules must come from this directory, not a registry or URL:" >&2
  echo "$remote" >&2
  status=1
fi

if [ "$status" -eq 0 ]; then
  echo "check-free-tier: $(echo "$used" | wc -w | tr -d ' ') resource types, all on the free-tier allowlist"
fi
exit "$status"
