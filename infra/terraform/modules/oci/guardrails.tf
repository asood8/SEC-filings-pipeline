# Spending guardrails, for a Pay As You Go account (docs/adr/0018-terraform.md, "Pay As You Go").
#
# A Pay As You Go account keeps Always Free resources free but can also create paid ones, and
# Oracle has no hard spending cap. So:
# - Quotas make Oracle refuse anything beyond what this module creates: Ampere A1 only, at most
#   the VM's OCPUs and memory in the whole tenancy, block storage within the free allowance, and
#   none of the services this project doesn't use. They are the real limit: a request over a
#   quota fails, whoever makes it.
# - A budget emails at the first cent actually spent, and when the month's forecast reaches the
#   budget. It only alerts; it stops nothing.

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
  # A1 limits are counted per availability domain and per region: both are capped, so the cap
  # holds across all three of Ashburn's domains. "zero" first, then the one shape allowed.
  quota_statements = [
    "zero compute-core quotas in tenancy",
    "set compute-core quota standard-a1-core-count to ${var.instance_ocpus} in tenancy",
    "set compute-core quota standard-a1-core-regional-count to ${var.instance_ocpus} in tenancy",
    "zero compute-memory quotas in tenancy",
    "set compute-memory quota standard-a1-memory-count to ${var.instance_memory_gbs} in tenancy",
    "set compute-memory quota standard-a1-memory-regional-count to ${var.instance_memory_gbs} in tenancy",
    "set block-storage quota total-storage-gb to ${var.free_block_storage_gbs} in tenancy",
    # Not used by this project: Postgres, Redpanda, and the rest run on the VM.
    "zero database quotas in tenancy",
    "zero mysql quotas in tenancy",
    "zero load-balancer quotas in tenancy",
    "zero filesystem quotas in tenancy",
  ]
}

resource "oci_limits_quota" "always_free" {
  compartment_id = var.tenancy_ocid # quotas live in the root compartment
  name           = "${var.name_prefix}-always-free"
  description    = "Caps the tenancy at what the SEC filings pipeline uses, all Always Free."
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
  message        = "Oracle Cloud charged money for the SEC filings pipeline's tenancy. Everything should be Always Free: check Cost Analysis for what it was."
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
