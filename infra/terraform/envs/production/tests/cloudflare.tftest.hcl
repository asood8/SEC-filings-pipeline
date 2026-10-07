# Tests of modules/cloudflare against a mock provider (no account, no credentials, nothing
# created): the tunnel's routing, the DNS records, the R2 buckets, and the Pages build.

mock_provider "cloudflare" {
  mock_data "cloudflare_zone" {
    defaults = {
      id = "0123456789abcdef0123456789abcdef"
    }
  }
}

variables {
  account_id = "fedcba9876543210fedcba9876543210"
  zone_name  = "example.com"
}

run "the_tunnel_routes_the_api_hostname_to_the_api_service" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared.api.config_src == "cloudflare"
    error_message = "The tunnel must be remotely managed, so cloudflared needs only its token."
  }

  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared_config.api.config.ingress[0].hostname == "api.example.com"
    error_message = "The first ingress rule must match the API hostname."
  }

  assert {
    # deploy/k8s/base/api.yaml: Service api, port 8000, in the filings namespace.
    condition     = cloudflare_zero_trust_tunnel_cloudflared_config.api.config.ingress[0].service == "http://api.filings.svc.cluster.local:8000"
    error_message = "The API hostname must reach the api Service on port 8000 in the filings namespace."
  }

  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared_config.api.config.ingress[1].service == "http_status:404"
    error_message = "Everything else must get a 404 (cloudflared requires a catch-all rule)."
  }

  assert {
    condition     = cloudflare_dns_record.api.name == "api.example.com" && cloudflare_dns_record.api.type == "CNAME" && cloudflare_dns_record.api.proxied
    error_message = "The API hostname must be a proxied CNAME to the tunnel."
  }

  assert {
    condition     = output.api_url == "https://api.example.com" && output.web_url == "https://app.example.com"
    error_message = "The public URLs should follow the subdomain variables."
  }
}

run "r2_buckets_are_free_standard_buckets_and_backups_expire" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  assert {
    # Names used by deploy/k8s/base/config.env (OBJECT_STORE_S3_BUCKET, BACKUP_S3_BUCKET).
    condition     = cloudflare_r2_bucket.raw.name == "filings-raw" && cloudflare_r2_bucket.backups.name == "filings-backups"
    error_message = "Bucket names must match the cluster's configuration."
  }

  assert {
    condition     = cloudflare_r2_bucket.raw.storage_class == "Standard" && cloudflare_r2_bucket.backups.storage_class == "Standard"
    error_message = "Only the Standard storage class is in R2's free tier."
  }

  assert {
    condition     = cloudflare_r2_bucket_lifecycle.backups.rules[0].delete_objects_transition.condition.max_age == 30 * 86400
    error_message = "Backups should expire after 30 days by default."
  }

  assert {
    condition     = output.r2_endpoint_url == "https://fedcba9876543210fedcba9876543210.r2.cloudflarestorage.com"
    error_message = "The S3 endpoint is per account."
  }
}

run "pages_builds_web_from_github_with_the_public_settings" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  variables {
    google_client_id = "1234567890-abc.apps.googleusercontent.com"
  }

  assert {
    condition = (
      cloudflare_pages_project.web.build_config.root_dir == "web" &&
      cloudflare_pages_project.web.build_config.build_command == "npm ci && npm run build" &&
      cloudflare_pages_project.web.build_config.destination_dir == "dist"
    )
    error_message = "Pages must build web/ with npm ci && npm run build into web/dist."
  }

  assert {
    condition     = cloudflare_pages_project.web.deployment_configs.production.env_vars["VITE_API_BASE_URL"].value == "https://api.example.com"
    error_message = "The website must call the API on its public hostname."
  }

  assert {
    condition     = cloudflare_pages_project.web.deployment_configs.production.env_vars["GOOGLE_CLIENT_ID"].value == "1234567890-abc.apps.googleusercontent.com"
    error_message = "The Google client ID must reach the build."
  }

  assert {
    condition     = cloudflare_pages_project.web.source.type == "github" && cloudflare_pages_project.web.source.config.path_includes == tolist(["web/**"])
    error_message = "Pages should build from GitHub, only when web/ changes."
  }

  assert {
    condition     = cloudflare_pages_project.web.source.config.preview_deployment_setting == "none"
    error_message = "Preview builds are off by default, to save the free plan's builds."
  }

  assert {
    condition     = cloudflare_pages_domain.web.name == "app.example.com"
    error_message = "The website is served on app.<domain>, beside the API (ADR-0013's cookie)."
  }
}

run "sign_in_is_left_out_of_the_build_until_a_client_id_is_set" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  assert {
    condition     = !contains(keys(cloudflare_pages_project.web.deployment_configs.production.env_vars), "GOOGLE_CLIENT_ID")
    error_message = "An empty GOOGLE_CLIENT_ID should not be sent to Pages."
  }
}

run "direct_upload_project_without_github" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  variables {
    pages_github_integration = false
  }

  assert {
    condition     = cloudflare_pages_project.web.source == null
    error_message = "Without the GitHub integration, the project has no source."
  }
}

# --- Refused configurations --------------------------------------------------------------------

run "refuses_infrequent_access_storage" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  variables {
    r2_storage_class = "InfrequentAccess"
  }

  expect_failures = [var.r2_storage_class]
}

run "refuses_unbounded_backup_retention" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  variables {
    backup_retention_days = 3650
  }

  expect_failures = [var.backup_retention_days]
}

run "refuses_a_malformed_domain" {
  command = plan

  module {
    source = "../../modules/cloudflare"
  }

  variables {
    zone_name = "https://example.com"
  }

  expect_failures = [var.zone_name]
}
