output "api_url" {
  description = "Public URL of the API (through the tunnel)."
  value       = "https://${local.api_hostname}"
}

output "web_url" {
  description = "Public URL of the website (Pages custom domain)."
  value       = "https://${local.web_hostname}"
}

output "pages_dev_url" {
  description = "The website's default pages.dev address."
  value       = "https://${cloudflare_pages_project.web.subdomain}"
}

output "tunnel_id" {
  description = "ID of the Cloudflare Tunnel."
  value       = cloudflare_zero_trust_tunnel_cloudflared.api.id
}

output "tunnel_token" {
  description = "Token for the cloudflared Deployment (TUNNEL_TOKEN in the cloudflared Secret). Anyone holding it can serve the API's hostname."
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.api.token
  sensitive   = true
}

output "r2_endpoint_url" {
  description = "S3 API endpoint of R2 for this account (OBJECT_STORE_S3_ENDPOINT_URL and R2_ENDPOINT_URL)."
  value       = "https://${var.account_id}.r2.cloudflarestorage.com"
}

output "r2_raw_bucket" {
  description = "Name of the raw filings bucket."
  value       = cloudflare_r2_bucket.raw.name
}

output "r2_backup_bucket" {
  description = "Name of the backups bucket."
  value       = cloudflare_r2_bucket.backups.name
}
