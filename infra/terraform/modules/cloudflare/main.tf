# Cloudflare, free plan: the tunnel that publishes the API, the DNS records, the R2 buckets, and
# the Pages project for the website.
#
# Left to a person in the dashboard, because each is an account action rather than configuration
# (infra/terraform/README.md, "Accounts and credentials"):
# - Adding the domain to Cloudflare and pointing its nameservers at Cloudflare.
# - Enabling R2 on the account (accepting its terms; Cloudflare asks for a payment method even
#   for the free tier, which is a decision for the user).
# - Creating R2 API tokens (S3 access keys) for the services, scoped to one bucket each.
# - Installing the Cloudflare Pages GitHub app on the repository, before the first apply.

locals {
  # Without a domain (zone_name null) only the account-level pieces exist: the tunnel, serving
  # nothing yet, and the R2 buckets. The DNS records and the website wait for the domain: the
  # site would have no API to call, and sign-in needs both on one domain (ADR-0013).
  has_domain   = var.zone_name != null
  api_hostname = local.has_domain ? "${var.api_subdomain}.${var.zone_name}" : null
  web_hostname = local.has_domain ? "${var.web_subdomain}.${var.zone_name}" : null
  api_url      = local.has_domain ? "https://${local.api_hostname}" : null
  web_url      = local.has_domain ? "https://${local.web_hostname}" : null

  # The api Service, reached by cloudflared from inside the cluster. Plain HTTP is fine: the hop
  # never leaves the node, and Cloudflare terminates TLS for visitors.
  api_service_url = "http://${var.api_service_name}.${var.api_namespace}.svc.cluster.local:${var.api_port}"

  # The website's build settings. Vite reads VITE_* and GOOGLE_CLIENT_ID at build time
  # (web/vite.config.ts) and bakes them into the static files, so only public values belong here.
  # GOOGLE_CLIENT_ID is left out while empty, which builds the site with sign-in switched off.
  pages_env_vars = merge(
    {
      VITE_API_BASE_URL = { type = "plain_text", value = local.api_url }
      NODE_VERSION      = { type = "plain_text", value = var.node_version }
    },
    var.google_client_id == "" ? {} : {
      GOOGLE_CLIENT_ID = { type = "plain_text", value = var.google_client_id }
    },
  )
}

data "cloudflare_zone" "main" {
  count = local.has_domain ? 1 : 0

  filter = {
    name = var.zone_name
  }
}

# --- Tunnel: https://api.<zone> -> the api Service --------------------------------------------

# A remotely managed tunnel: its routing lives at Cloudflare (the config resource below), and the
# cloudflared Deployment (deploy/k8s/base/cloudflared.yaml) only needs the tunnel token. Its
# connections are outbound, so the VM opens no ports.
resource "cloudflare_zero_trust_tunnel_cloudflared" "api" {
  account_id = var.account_id
  name       = var.tunnel_name
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "api" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.api.id

  config = {
    # Without a domain, only the catch-all: cloudflared runs, but nothing is public yet.
    ingress = slice([
      {
        hostname = local.api_hostname
        service  = local.api_service_url
      },
      # cloudflared requires a final catch-all rule: anything else gets a 404.
      {
        hostname = null
        service  = "http_status:404"
      },
    ], local.has_domain ? 0 : 1, 2)
  }
}

# The token the cloudflared pods run with (TUNNEL_TOKEN in the cloudflared Secret).
data "cloudflare_zero_trust_tunnel_cloudflared_token" "api" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.api.id
}

resource "cloudflare_dns_record" "api" {
  count = local.has_domain ? 1 : 0

  zone_id = data.cloudflare_zone.main[0].id
  name    = local.api_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.api.id}.cfargotunnel.com"
  proxied = true # required for a tunnel: the record only works through Cloudflare's proxy
  ttl     = 1    # "automatic", the only TTL for proxied records
  comment = "Cloudflare Tunnel to the filings API (Terraform)"
}

# --- R2: raw filings and Postgres backups -----------------------------------------------------

resource "cloudflare_r2_bucket" "raw" {
  account_id    = var.account_id
  name          = var.r2_raw_bucket_name
  location      = var.r2_location
  storage_class = var.r2_storage_class
}

resource "cloudflare_r2_bucket" "backups" {
  account_id    = var.account_id
  name          = var.r2_backup_bucket_name
  location      = var.r2_location
  storage_class = var.r2_storage_class
}

# Backups expire after backup_retention_days, which keeps storage bounded and inside the free
# tier. Raw filings have no expiry: they are small, and keeping them is what allows reparsing
# without asking SEC again.
resource "cloudflare_r2_bucket_lifecycle" "backups" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.backups.name

  rules = [
    {
      id         = "expire-backups"
      enabled    = true
      conditions = { prefix = "" }
      delete_objects_transition = {
        condition = {
          type    = "Age"
          max_age = var.backup_retention_days * 24 * 60 * 60 # seconds
        }
      }
      # Clean up uploads that were interrupted halfway, which are billed as storage too.
      abort_multipart_uploads_transition = {
        condition = {
          type    = "Age"
          max_age = 7 * 24 * 60 * 60
        }
      }
    },
  ]
}

# --- Pages: the website ----------------------------------------------------------------------

resource "cloudflare_pages_project" "web" {
  count = local.has_domain ? 1 : 0

  account_id        = var.account_id
  name              = var.pages_project_name
  production_branch = var.production_branch

  build_config = {
    root_dir        = "web"
    build_command   = "npm ci && npm run build"
    destination_dir = "dist" # web/dist
    build_caching   = true
  }

  deployment_configs = {
    production = { env_vars = local.pages_env_vars }
    preview    = { env_vars = local.pages_env_vars }
  }

  source = var.pages_github_integration ? {
    type = "github"
    config = {
      owner                          = var.github_owner
      repo_name                      = var.github_repo
      production_branch              = var.production_branch
      production_deployments_enabled = true
      preview_deployment_setting     = var.pages_preview_deployments ? "all" : "none"
      pr_comments_enabled            = var.pages_preview_deployments
      # Build only when the website changed, to stay well inside 500 builds a month.
      path_includes = ["web/**"]
    }
  } : null
}

# The website on https://app.<zone>: Pages issues the certificate once the CNAME below resolves.
resource "cloudflare_pages_domain" "web" {
  count = local.has_domain ? 1 : 0

  account_id   = var.account_id
  project_name = cloudflare_pages_project.web[0].name
  name         = local.web_hostname
}

resource "cloudflare_dns_record" "web" {
  count = local.has_domain ? 1 : 0

  zone_id = data.cloudflare_zone.main[0].id
  name    = local.web_hostname
  type    = "CNAME"
  content = cloudflare_pages_project.web[0].subdomain
  proxied = true
  ttl     = 1
  comment = "Cloudflare Pages: the filings website (Terraform)"
}
