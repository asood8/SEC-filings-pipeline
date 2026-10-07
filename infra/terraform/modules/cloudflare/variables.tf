# Cloudflare free plan (brief §9, checked October 2026; re-verify before applying):
# - Tunnel, DNS, and Pages are free. Pages allows 500 builds a month and one build at a time.
# - R2's free tier is 10 GB-month of Standard storage, 1 million Class A and 10 million Class B
#   operations a month, and free egress. Infrequent Access storage is not in the free tier.
# Nothing in this module uses a paid feature (load balancing, Argo Smart Routing, Workers paid
# plans, Access seats beyond the free 50, ...).

variable "account_id" {
  description = "Cloudflare account ID (dashboard: any zone's Overview page, or Account home)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.account_id))
    error_message = "account_id must be a 32-character hexadecimal Cloudflare account ID."
  }
}

variable "zone_name" {
  description = "The domain (a zone already active in this Cloudflare account), for example example.com. The domain is an open decision (brief §12)."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.zone_name))
    error_message = "zone_name must be a lowercase domain name such as example.com."
  }
}

variable "api_subdomain" {
  description = "Label of the API's hostname under zone_name (api gives api.<zone>)."
  type        = string
  default     = "api"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.api_subdomain))
    error_message = "api_subdomain must be a single DNS label."
  }
}

variable "web_subdomain" {
  description = "Label of the website's hostname under zone_name (app gives app.<zone>). The website and the API must share the domain for the session cookie (ADR-0013)."
  type        = string
  default     = "app"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.web_subdomain))
    error_message = "web_subdomain must be a single DNS label."
  }
}

# --- Tunnel ---------------------------------------------------------------------------------

variable "tunnel_name" {
  description = "Name of the Cloudflare Tunnel."
  type        = string
  default     = "filings"
}

variable "api_service_name" {
  description = "Kubernetes Service the tunnel sends API traffic to (deploy/k8s/base/api.yaml)."
  type        = string
  default     = "api"
}

variable "api_namespace" {
  description = "Namespace of that Service (deploy/k8s/base/namespace.yaml)."
  type        = string
  default     = "filings"
}

variable "api_port" {
  description = "Port of that Service."
  type        = number
  default     = 8000

  validation {
    condition     = var.api_port >= 1 && var.api_port <= 65535
    error_message = "api_port must be a TCP port number."
  }
}

# --- R2 -------------------------------------------------------------------------------------

variable "r2_raw_bucket_name" {
  description = "R2 bucket for raw filings (OBJECT_STORE_S3_BUCKET in deploy/k8s/base/config.env)."
  type        = string
  default     = "filings-raw"
}

variable "r2_backup_bucket_name" {
  description = "R2 bucket for the nightly Postgres backups (BACKUP_S3_BUCKET in deploy/k8s/base/config.env)."
  type        = string
  default     = "filings-backups"
}

variable "r2_location" {
  description = "R2 location hint (apac, eeur, enam, weur, wnam, oc): pick the one nearest the VM's region. null lets Cloudflare choose."
  type        = string
  default     = null

  validation {
    condition     = var.r2_location == null || contains(["apac", "eeur", "enam", "weur", "wnam", "oc"], coalesce(var.r2_location, "unset"))
    error_message = "r2_location must be null or one of apac, eeur, enam, weur, wnam, oc."
  }
}

variable "r2_storage_class" {
  description = "Default storage class of both buckets. Only Standard is in R2's free tier."
  type        = string
  default     = "Standard"

  validation {
    condition     = var.r2_storage_class == "Standard"
    error_message = "Only the Standard storage class is free; Infrequent Access bills storage and retrieval."
  }
}

variable "backup_retention_days" {
  description = "Days a Postgres backup is kept before R2 deletes it (the backup job itself never deletes)."
  type        = number
  default     = 30

  validation {
    # Each dump is small (tens of MB at this project's size), so even 90 nightly copies stay far
    # below the 10 GB of free storage, which raw filings share.
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 90 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "backup_retention_days must be a whole number of days from 1 to 90."
  }
}

# --- Pages ----------------------------------------------------------------------------------

variable "pages_project_name" {
  description = "Cloudflare Pages project name; the site is also served on <name>.pages.dev (with a suffix if the name is taken)."
  type        = string
  default     = "filings"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,56}[a-z0-9])?$", var.pages_project_name))
    error_message = "pages_project_name must be lowercase letters, digits, and hyphens (at most 58 characters)."
  }
}

variable "production_branch" {
  description = "Git branch Pages deploys to production."
  type        = string
  default     = "main"
}

variable "pages_github_integration" {
  description = <<-EOT
    true: Pages builds the site from GitHub on every push that touches web/. Requires the
    Cloudflare Pages GitHub app to be installed on the repository first (a dashboard step).
    false: a Direct Upload project, which a CI job must deploy with Wrangler (and a Cloudflare
    API token in GitHub's secrets). A project can't be switched from one kind to the other later.
  EOT
  type        = bool
  default     = true
}

variable "github_owner" {
  description = "GitHub owner of the repository Pages builds from."
  type        = string
  default     = "asood8"
}

variable "github_repo" {
  description = "GitHub repository Pages builds from."
  type        = string
  default     = "SEC-filings-pipeline"
}

variable "pages_preview_deployments" {
  description = "Also build a preview for every other branch and pull request. Off by default, to save the free plan's 500 builds a month."
  type        = bool
  default     = false
}

variable "google_client_id" {
  description = "Google OAuth client ID for sign-in, built into the website (public, not a secret). Empty disables sign-in."
  type        = string
  default     = ""

  validation {
    condition     = var.google_client_id == "" || endswith(var.google_client_id, ".apps.googleusercontent.com")
    error_message = "google_client_id must be empty or a client ID ending in .apps.googleusercontent.com."
  }
}

variable "node_version" {
  description = "Node.js version for the Pages build (the production environment passes web/.nvmrc's)."
  type        = string
  default     = "24"

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+){0,2}$", var.node_version))
    error_message = "node_version must be a Node.js version such as 24 or 24.9.0."
  }
}
