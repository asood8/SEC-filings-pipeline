# Versions of Terraform and of every provider this configuration may use.
#
# - Terraform: any 1.x from 1.10. CI pins the release in .github/workflows/ci.yml (1.16.5);
#   develop with the same one. OpenTofu 1.10+ also satisfies this constraint (ADR-0018).
# - Providers: `~> X.Y` accepts bug-fix and feature releases of one major version, never a new
#   major. The exact versions and their checksums are recorded in .terraform.lock.hcl, which is
#   committed: `terraform init` installs those and nothing else. Upgrade deliberately with
#   `terraform init -upgrade`, then review and commit the lock file.
#
# State: there is no backend block, so the state is a local file, terraform.tfstate, next to
# this configuration (gitignored). It contains secrets; keep it private and back it up. Moving it
# to a remote backend is an open decision: README.md, "State".
terraform {
  required_version = ">= 1.10.0, < 2.0.0"

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
