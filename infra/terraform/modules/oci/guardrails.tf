# Spending guardrails (docs/adr/0018-terraform.md, "Guardrails against charges"). The free account
# refuses anything beyond Always Free, but its free trial and a Pay As You Go upgrade don't: they
# create what is asked and bill what exceeds the allowance, and Oracle has no spending cap. So:
# - A quota policy makes Oracle itself refuse anything beyond what this project uses: Ampere A1
#   only, at most the free 2 OCPU and 12 GB in the whole tenancy (whatever size the VM has now, so
#   it can still grow; ADR-0020), block storage within the free allowance in each availability
#   domain, no volume backups or replicas, and nothing from the services listed below. A request
#   over a quota fails, whoever makes it: Terraform, the console, or a script.
# - A budget emails at the first cent actually spent, and when the month's forecast reaches $1.
#   It only alerts; it stops nothing. That is why the quotas come first.

variable "budget_alert_emails" {
  description = "Who the budget emails. Empty: no budget is created (the quotas still are)."
  type        = list(string)
  default     = []
  sensitive   = true
}

variable "budget_amount" {
  description = "The monthly budget in the account's currency. Everything here should cost 0."
  type        = number
  default     = 1
}

locals {
  # The Always Free Ampere A1 allowance (brief §9), for the whole tenancy.
  free_a1_ocpus      = 2
  free_a1_memory_gbs = 12

  # Services this project doesn't use, each of which can cost money. All of them support quotas
  # (checked with Oracle's Limits API, 2026-10-25). Not here: what the project uses or may need
  # (vcn, vcnip, email-delivery, logging, notifications), DNS (the VCN's resolver lives there),
  # and services without quotas (mysql, monitoring), which only the budget watches.
  unused_services = [
    "ai-generative",
    "analytics",
    "api-gateway",
    "apm",
    "auto-scaling",
    "big-data",
    "compute-management",
    "container-engine",
    "data-flow",
    "data-science",
    "database",
    "faas",
    "fast-connect",
    "filestoragenetapp",
    "filesystem",
    "generative-ai-agent",
    "goldengate",
    "integration",
    "kms",
    "load-balancer",
    "lustrefilestorage",
    "network-firewall",
    "network-load-balancer-api",
    "nosql",
    "object-storage",
    "oci-kafka",
    "ocvp",
    "open-search",
    "postgresql",
    "queue",
    "redis",
    "streaming",
    "vpn",
    "waas",
    "waf",
  ]

  # "zero" first, then what is allowed. A1 is capped per availability domain and region-wide (the
  # limit exists at both levels; only the regional one holds across Ashburn's three domains).
  # Block storage has no region-wide quota, so each domain gets the whole allowance; Terraform's
  # own check (boot + data within free_block_storage_gbs) and the budget cover the rest.
  quota_statements = concat(
    [
      "zero compute-core quotas in tenancy",
      "set compute-core quota standard-a1-core-count to ${local.free_a1_ocpus} in tenancy",
      "set compute-core quota standard-a1-core-regional-count to ${local.free_a1_ocpus} in tenancy",
      "zero compute-memory quotas in tenancy",
      "set compute-memory quota standard-a1-memory-count to ${local.free_a1_memory_gbs} in tenancy",
      "set compute-memory quota standard-a1-memory-regional-count to ${local.free_a1_memory_gbs} in tenancy",
      "zero block-storage quotas in tenancy",
      "set block-storage quota total-storage-gb to ${var.free_block_storage_gbs} in tenancy",
      # The boot volume and the data volume.
      "set block-storage quota volume-count to 2 in tenancy",
    ],
    [for service in local.unused_services : "zero ${service} quotas in tenancy"],
  )
}

resource "oci_limits_quota" "always_free" {
  compartment_id = var.tenancy_ocid # quotas live in the root compartment
  name           = "${var.name_prefix}-always-free"
  description    = "Caps the tenancy at what the SEC filings pipeline uses, all Always Free (docs/adr/0018)."
  statements     = local.quota_statements
  freeform_tags  = var.freeform_tags
}

resource "oci_budget_budget" "stay_free" {
  count = length(var.budget_alert_emails) > 0 ? 1 : 0

  compartment_id = var.tenancy_ocid
  display_name   = "${var.name_prefix}-stay-free"
  description    = "Everything in this tenancy should be Always Free: any spend is a mistake."
  amount         = var.budget_amount
  reset_period   = "MONTHLY"
  target_type    = "COMPARTMENT"
  targets        = [var.tenancy_ocid]
  freeform_tags  = var.freeform_tags
}

# 1% of the budget: the first cent actually charged.
resource "oci_budget_alert_rule" "any_spend" {
  count = length(oci_budget_budget.stay_free)

  budget_id      = oci_budget_budget.stay_free[0].id
  display_name   = "any-actual-spend"
  type           = "ACTUAL"
  threshold_type = "PERCENTAGE"
  threshold      = 1
  recipients     = join(",", var.budget_alert_emails)
  message        = "Oracle Cloud charged money for the SEC filings pipeline's tenancy. Everything should be Always Free: check Cost Analysis for what it was, and remove it."
}

resource "oci_budget_alert_rule" "forecast" {
  count = length(oci_budget_budget.stay_free)

  budget_id      = oci_budget_budget.stay_free[0].id
  display_name   = "forecast-reaches-budget"
  type           = "FORECAST"
  threshold_type = "PERCENTAGE"
  threshold      = 100
  recipients     = join(",", var.budget_alert_emails)
  message        = "Oracle Cloud forecasts spend this month for the SEC filings pipeline's tenancy. Everything should be Always Free: check Cost Analysis."
}
