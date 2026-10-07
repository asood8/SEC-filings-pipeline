# Provider credentials are never written in these files. Each comes from a sensitive variable
# (set in a gitignored *.tfvars file or as TF_VAR_<name> in the environment) or, when that is
# left null, from the provider's own environment variable or config file. README.md lists them.

# OCI: an API signing key in ~/.oci/config (created by `oci setup config` or the console's
# "Add API key" page). Always Free resources must be created in the tenancy's home region.
provider "oci" {
  auth                = "APIKey"
  config_file_profile = var.oci_config_file_profile
  region              = var.oci_region
}

# Cloudflare: an API token (null -> CLOUDFLARE_API_TOKEN).
provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# MongoDB Atlas: an organization service account (null -> MONGODB_ATLAS_CLIENT_ID and
# MONGODB_ATLAS_CLIENT_SECRET).
provider "mongodbatlas" {
  client_id     = var.atlas_client_id
  client_secret = var.atlas_client_secret
}

# Grafana Cloud: the stack's URL and a service account token (null -> GRAFANA_URL, GRAFANA_AUTH).
provider "grafana" {
  url  = var.grafana_url
  auth = var.grafana_auth
}
