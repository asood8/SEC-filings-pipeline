# Inputs of the production environment. Copy terraform.tfvars.example to terraform.tfvars
# (gitignored) and fill it in. Free-tier limits are enforced by the modules' own validations.

# --- Oracle Cloud ---------------------------------------------------------------------------

variable "oci_region" {
  description = "The tenancy's home region, such as us-ashburn-1 (Always Free resources exist only there)."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+-[0-9]+$", var.oci_region))
    error_message = "oci_region must be an OCI region identifier such as us-ashburn-1."
  }
}

variable "oci_config_file_profile" {
  description = "Profile in ~/.oci/config holding the API signing key Terraform uses."
  type        = string
  default     = "DEFAULT"
}

variable "oci_tenancy_ocid" {
  description = "OCID of the tenancy."
  type        = string
}

variable "oci_compartment_ocid" {
  description = "OCID of the compartment for every OCI resource. null uses the root compartment (the tenancy)."
  type        = string
  default     = null
}

variable "oci_availability_domain_number" {
  description = "Availability domain (1-3) for the VM; change it if A1 capacity is exhausted in one."
  type        = number
  default     = 1
}

variable "data_volume_size_gbs" {
  description = "Size of the block volume for k3s's persistent volumes, in GB (boot + data must stay within 200 GB)."
  type        = number
  default     = 100
}

variable "ssh_ingress_cidr" {
  description = "Source range allowed to reach SSH, such as x.x.x.x/32. null (default) keeps every inbound port closed."
  type        = string
  default     = null
}

variable "ssh_authorized_keys" {
  description = "Public SSH key(s) for the ubuntu user. Not a secret, but personal: set it in terraform.tfvars."
  type        = string
  default     = ""
}

variable "k3s_version" {
  description = "k3s release cloud-init installs."
  type        = string
  default     = "v1.36.5+k3s1"
}

variable "git_revision" {
  description = "Branch, tag, or commit whose deploy/argocd manifests cloud-init applies on the first boot."
  type        = string
  default     = "main"
}

# --- Cloudflare -----------------------------------------------------------------------------

variable "cloudflare_api_token" {
  description = "Cloudflare API token for Terraform (README.md lists its permissions). null reads CLOUDFLARE_API_TOKEN."
  type        = string
  default     = null
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID."
  type        = string
}

variable "domain" {
  description = "The domain, already added to Cloudflare as a zone (an open decision, brief §12)."
  type        = string
}

variable "api_subdomain" {
  description = "The API is served on <api_subdomain>.<domain>."
  type        = string
  default     = "api"
}

variable "web_subdomain" {
  description = "The website is served on <web_subdomain>.<domain>."
  type        = string
  default     = "app"
}

variable "r2_location" {
  description = "R2 location hint nearest the VM (apac, eeur, enam, weur, wnam, oc); null lets Cloudflare choose."
  type        = string
  default     = null
}

variable "backup_retention_days" {
  description = "Days R2 keeps each nightly Postgres backup."
  type        = number
  default     = 30
}

variable "pages_github_integration" {
  description = "Build the website on Pages from GitHub (needs the Cloudflare Pages GitHub app installed first). false makes a Direct Upload project instead."
  type        = bool
  default     = true
}

variable "google_client_id" {
  description = "Google OAuth client ID for sign-in (public). Empty disables sign-in on the website."
  type        = string
  default     = ""
}

# --- MongoDB Atlas --------------------------------------------------------------------------

variable "atlas_client_id" {
  description = "Client ID of an Atlas organization service account. null reads MONGODB_ATLAS_CLIENT_ID."
  type        = string
  default     = null
  sensitive   = true
}

variable "atlas_client_secret" {
  description = "Client secret of that service account. null reads MONGODB_ATLAS_CLIENT_SECRET."
  type        = string
  default     = null
  sensitive   = true
}

variable "atlas_org_id" {
  description = "Atlas organization ID."
  type        = string
}

variable "atlas_backing_provider" {
  description = "Cloud hosting the free M0 cluster: AWS, GCP, or AZURE."
  type        = string
  default     = "AWS"
}

variable "atlas_region" {
  description = "Atlas region of the M0 cluster (Atlas naming), near the VM's region."
  type        = string
  default     = "US_EAST_1"
}

variable "atlas_extra_access" {
  description = "Extra addresses allowed to reach Atlas besides the VM, as name => CIDR (for example an operator's x.x.x.x/32 while debugging)."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for cidr in values(var.atlas_extra_access) : can(cidrhost(cidr, 0))])
    error_message = "atlas_extra_access values must be CIDR blocks such as 203.0.113.7/32."
  }
}

# --- Grafana Cloud --------------------------------------------------------------------------

variable "grafana_url" {
  description = "URL of the existing Grafana Cloud stack, such as https://<stack>.grafana.net. null reads GRAFANA_URL."
  type        = string
  default     = null
}

variable "grafana_auth" {
  description = "Service account token of that stack, with permission to manage dashboards, folders, and alert rules. null reads GRAFANA_AUTH."
  type        = string
  default     = null
  sensitive   = true
}

variable "grafana_datasource_uids" {
  description = "UIDs of the stack's metrics, logs, and traces data sources, if they differ from Grafana Cloud's usual ones."
  type = object({
    prometheus = optional(string, "grafanacloud-prom")
    loki       = optional(string, "grafanacloud-logs")
    tempo      = optional(string, "grafanacloud-traces")
  })
  default = {}
}
