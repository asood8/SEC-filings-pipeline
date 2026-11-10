# A user who can only look, for the daily inventory of the account (filings-oci-inventory,
# docs/adr/0018-terraform.md): it lists what exists and reads the quota policy and the budget,
# so the check can alert on anything unexpected long before Oracle's daily budget alert would.
# Identity resources cost nothing. The key pair is made by hand, so its private half never
# enters Terraform's state (infra/terraform/README.md, "Secrets into the cluster"):
#
#   openssl genrsa -out ~/.config/filings/oci_inventory_key.pem 2048
#   openssl rsa -in ~/.config/filings/oci_inventory_key.pem -pubout -out ~/.config/filings/oci_inventory_key.pub.pem

variable "inventory_public_key_path" {
  description = "PEM public key of the read-only inventory user's API key. Null: no such user is created."
  type        = string
  default     = null
}

variable "inventory_user_email" {
  description = "Email for the inventory user (Oracle requires one per user; it must differ from other users'). Null: the first budget alert email."
  type        = string
  default     = null
  sensitive   = true
}

locals {
  inventory       = var.inventory_public_key_path != null
  inventory_email = try(coalesce(var.inventory_user_email, one(slice(var.budget_alert_emails, 0, 1))), null)
}

resource "oci_identity_group" "inventory" {
  count = local.inventory ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = "${var.name_prefix}-inventory"
  description    = "Reads what exists in the tenancy, for the daily inventory check. Changes nothing."
  freeform_tags  = var.freeform_tags
}

resource "oci_identity_user" "inventory" {
  count = local.inventory ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = "${var.name_prefix}-inventory"
  description    = "The daily inventory check's API user (read-only)."
  email          = local.inventory_email
  freeform_tags  = var.freeform_tags

  lifecycle {
    precondition {
      condition     = local.inventory_email != null
      error_message = "The inventory user needs an email: set inventory_user_email or budget_alert_emails."
    }
  }
}

resource "oci_identity_user_group_membership" "inventory" {
  count = local.inventory ? 1 : 0

  group_id = oci_identity_group.inventory[0].id
  user_id  = oci_identity_user.inventory[0].id
}

resource "oci_identity_policy" "inventory" {
  count = local.inventory ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = "${var.name_prefix}-inventory-read-only"
  description    = "The inventory user may list every resource, and read the quota policy and the budget. Nothing else."
  statements = [
    "allow group ${oci_identity_group.inventory[0].name} to inspect all-resources in tenancy",
    "allow group ${oci_identity_group.inventory[0].name} to read quotas in tenancy",
    "allow group ${oci_identity_group.inventory[0].name} to read usage-budgets in tenancy",
  ]
  freeform_tags = var.freeform_tags
}

resource "oci_identity_api_key" "inventory" {
  count = local.inventory ? 1 : 0

  user_id   = oci_identity_user.inventory[0].id
  key_value = file(pathexpand(var.inventory_public_key_path))
}

output "inventory_user" {
  description = "The inventory user's OCID and its API key's fingerprint, for the oci-inventory Secret."
  value = local.inventory ? {
    user_ocid   = oci_identity_user.inventory[0].id
    fingerprint = oci_identity_api_key.inventory[0].fingerprint
  } : null
}
