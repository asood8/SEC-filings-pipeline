# Read with `terraform output`. Sensitive values are hidden there; print one with
# `terraform output -raw <name>` only when piping it straight into kubectl/kubeseal (README.md,
# "Secrets into the cluster"), never into a file in the repository.

# --- The VM ---

output "vm_public_ip" {
  description = "Public IP of the k3s VM (no inbound ports unless ssh_ingress_cidr is set)."
  value       = module.oci.public_ip
}

output "vm_instance_id" {
  description = "OCID of the k3s VM (for the console, serial console, or `oci` CLI)."
  value       = module.oci.instance_id
}

output "vm_image" {
  description = "Ubuntu image the VM was created from."
  value       = module.oci.image_name
}

output "ssh_command" {
  description = "How to reach the VM while SSH is open."
  value       = var.ssh_ingress_cidr == null ? "SSH is closed (set ssh_ingress_cidr to open it)" : "ssh ubuntu@${module.oci.public_ip}"
}

# --- Public addresses ---

output "api_url" {
  description = "The API's public URL."
  value       = module.cloudflare.api_url
}

output "web_url" {
  description = "The website's public URL."
  value       = module.cloudflare.web_url
}

output "pages_dev_url" {
  description = "The website's pages.dev address."
  value       = module.cloudflare.pages_dev_url
}

output "api_cors_origins" {
  description = "Value for API_CORS_ORIGINS in deploy/k8s/overlays/production/config.env."
  value       = jsonencode(module.cloudflare.web_url == null ? [] : [module.cloudflare.web_url])
}

# --- Values for the cluster's Secrets (README.md, "Secrets into the cluster") ---

output "tunnel_token" {
  description = "TUNNEL_TOKEN for the cloudflared Secret."
  value       = module.cloudflare.tunnel_token
  sensitive   = true
}

output "mongodb_url" {
  description = "MONGODB_URL for the mongodb Secret."
  value       = module.atlas.connection_string
  sensitive   = true
}

output "r2_endpoint_url" {
  description = "OBJECT_STORE_S3_ENDPOINT_URL and R2_ENDPOINT_URL for the r2-raw-filings and r2-backups Secrets."
  value       = module.cloudflare.r2_endpoint_url
}

output "r2_buckets" {
  description = "R2 bucket names (they match OBJECT_STORE_S3_BUCKET and BACKUP_S3_BUCKET in deploy/k8s/base/config.env)."
  value = {
    raw_filings = module.cloudflare.r2_raw_bucket
    backups     = module.cloudflare.r2_backup_bucket
  }
}

# --- Observability ---

output "grafana_dashboards" {
  description = "URLs of the provisioned Grafana dashboards."
  value       = module.grafana.dashboard_urls
}

output "grafana_rule_groups" {
  description = "Provisioned Grafana alert rule groups."
  value       = module.grafana.rule_groups
}
