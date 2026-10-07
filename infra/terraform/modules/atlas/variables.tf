# MongoDB Atlas free tier (brief §9, checked October 2026; re-verify before applying): one M0
# cluster per project, 512 MB of storage, free forever, no credit card. An M0 cluster pauses after
# 60 days without connections; the parser and summarizer connect all the time. Every other tier
# (Flex, M10 and up) is billed, so the cluster is locked to M0 below.

variable "org_id" {
  description = "Atlas organization ID (Organization Settings in the Atlas UI)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{24}$", var.org_id))
    error_message = "org_id must be a 24-character hexadecimal Atlas organization ID."
  }
}

variable "project_name" {
  description = "Name of the Atlas project to create."
  type        = string
  default     = "filings"
}

variable "cluster_name" {
  description = "Name of the M0 cluster."
  type        = string
  default     = "filings"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]{0,22}$", var.cluster_name))
    error_message = "cluster_name must be 1-23 letters, digits, or hyphens, starting with a letter or digit."
  }
}

variable "instance_size" {
  description = "Cluster tier. Only M0, the free tier, is allowed."
  type        = string
  default     = "M0"

  validation {
    condition     = var.instance_size == "M0"
    error_message = "Only M0 is free. Flex, M10, and larger tiers are billed."
  }
}

variable "provider_name" {
  description = "Atlas provider name. Free (shared) clusters use TENANT, with the cloud given by backing_provider_name."
  type        = string
  default     = "TENANT"

  validation {
    condition     = var.provider_name == "TENANT"
    error_message = "provider_name must be TENANT: that is how Atlas runs free M0 clusters. AWS, GCP, or AZURE here would create a dedicated, billed cluster."
  }
}

variable "backing_provider_name" {
  description = "Cloud that hosts the M0 cluster: AWS, GCP, or AZURE."
  type        = string
  default     = "AWS"

  validation {
    condition     = contains(["AWS", "GCP", "AZURE"], var.backing_provider_name)
    error_message = "backing_provider_name must be AWS, GCP, or AZURE."
  }
}

variable "region_name" {
  description = "Atlas region for the cluster (Atlas naming, such as US_EAST_1). Pick one near the VM's OCI region that offers M0."
  type        = string
  default     = "US_EAST_1"

  validation {
    condition     = can(regex("^[A-Z0-9_]+$", var.region_name))
    error_message = "region_name must use Atlas's naming, for example US_EAST_1 or EU_WEST_1."
  }
}

variable "database_name" {
  description = "Database the application uses (MONGODB_DATABASE in deploy/k8s/base/config.env); the user gets readWrite on it only."
  type        = string
  default     = "filings"
}

variable "database_username" {
  description = "Name of the application's database user."
  type        = string
  default     = "filings"
}

variable "access_list" {
  description = <<-EOT
    Addresses allowed to connect, as a map from a short name to a CIDR block: the VM's public IP
    as x.x.x.x/32, plus any temporary ones (an operator's address while debugging). A map, not a
    list, because the VM's address is only known after it is created.
  EOT
  type        = map(string)

  validation {
    # IPv4 blocks of /16 or narrower: never 0.0.0.0/0 (anyone). Values unknown at plan time, such
    # as the VM's address before it exists, are checked during the apply.
    condition = alltrue([
      for cidr in values(var.access_list) :
      can(cidrnetmask(cidr)) && try(tonumber(split("/", cidr)[1]) >= 16, false)
    ])
    error_message = "access_list values must be IPv4 CIDR blocks of /16 or narrower, such as 203.0.113.7/32; 0.0.0.0/0 (anyone) is refused."
  }
}
