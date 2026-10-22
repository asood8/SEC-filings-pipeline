# Versions of Terraform and of every provider this configuration may use.
#
# - Terraform: any 1.x from 1.10. CI pins the release in .github/workflows/ci.yml (1.16.5);
#   develop with the same one. OpenTofu 1.10+ also satisfies this constraint (ADR-0018).
# - Providers: `~> X.Y` accepts bug-fix and feature releases of one major version, never a new
#   major. The exact versions and their checksums are recorded in .terraform.lock.hcl, which is
#   committed: `terraform init` installs those and nothing else. Upgrade deliberately with
#   `terraform init -upgrade`, then review and commit the lock file.
#
# State: in the private R2 bucket filings-tfstate, made by hand before the first init (README.md,
# "State"). The account's endpoint isn't committed: it comes from production.s3.tfbackend
# (gitignored; template: production.s3.tfbackend.example), and the bucket's token from
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
#   terraform init -backend-config=production.s3.tfbackend
# R2 supports the conditional writes Terraform's lock file needs (checked 2026-10-22). The checks
# and tests run with `init -backend=false` and need no credentials.
terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  backend "s3" {
    bucket                      = "filings-tfstate"
    key                         = "production/terraform.tfstate"
    region                      = "auto"
    use_lockfile                = true
    use_path_style              = true
    skip_region_validation      = true
    skip_credentials_validation = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 9.8"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.27"
    }
    mongodbatlas = {
      source  = "mongodb/mongodbatlas"
      version = "~> 2.19"
    }
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.47"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}
