# Tests of the production root module: the four modules wired together, against mock providers
# (no accounts, no credentials, nothing created). The provider blocks in providers.tf are replaced
# by these mocks, so no credential is read either.

mock_provider "oci" {
  mock_data "oci_identity_availability_domains" {
    defaults = {
      availability_domains = [{ name = "Uocm:US-ASHBURN-AD-1" }]
    }
  }

  mock_data "oci_core_images" {
    defaults = {
      images = [{ id = "ocid1.image.oc1.iad.mock", display_name = "Canonical-Ubuntu-24.04-aarch64-2026.09.30-0" }]
    }
  }
}

mock_provider "cloudflare" {}
mock_provider "mongodbatlas" {}
mock_provider "grafana" {}

variables {
  oci_region            = "us-ashburn-1"
  oci_tenancy_ocid      = "ocid1.tenancy.oc1..mock"
  cloudflare_account_id = "fedcba9876543210fedcba9876543210"
  domain                = "example.com"
  atlas_org_id          = "0123456789abcdef01234567"
}

run "wires_the_modules_together" {
  command = plan

  # The VM's public IP is only known after it is created; give the plan one.
  override_resource {
    target          = module.oci.oci_core_instance.k3s
    override_during = plan
    values = {
      public_ip = "203.0.113.10"
    }
  }

  assert {
    condition     = module.atlas.access_list_cidrs == toset(["203.0.113.10/32"])
    error_message = "Atlas must admit the VM's public IP, and nothing else by default."
  }

  assert {
    condition     = output.api_url == "https://api.example.com" && output.web_url == "https://app.example.com"
    error_message = "The public URLs follow the domain."
  }

  assert {
    # The value deploy/k8s/overlays/production/config.env needs for API_CORS_ORIGINS.
    condition     = output.api_cors_origins == "[\"https://app.example.com\"]"
    error_message = "api_cors_origins must be a JSON list holding the website's origin."
  }

  assert {
    condition     = output.ssh_command == "SSH is closed (set ssh_ingress_cidr to open it)"
    error_message = "SSH is closed by default."
  }

  assert {
    # deploy/grafana/dashboards may not exist yet; then both sides are zero.
    condition     = length(output.grafana_dashboards) == length(fileset("../../../../deploy/grafana/dashboards", "*.json"))
    error_message = "Every dashboard file in deploy/grafana/dashboards must be provisioned."
  }
}

run "extra_atlas_addresses_join_the_vm" {
  command = plan

  variables {
    atlas_extra_access = { laptop = "198.51.100.4/32" }
  }

  override_resource {
    target          = module.oci.oci_core_instance.k3s
    override_during = plan
    values = {
      public_ip = "203.0.113.10"
    }
  }

  assert {
    condition     = module.atlas.access_list_cidrs == toset(["203.0.113.10/32", "198.51.100.4/32"])
    error_message = "atlas_extra_access entries are added beside the VM."
  }
}

run "refuses_a_malformed_region" {
  command = plan

  variables {
    oci_region = "Ashburn"
  }

  expect_failures = [var.oci_region]
}

run "a_first_apply_without_a_domain" {
  command = plan

  variables {
    domain = null
  }

  assert {
    condition     = output.api_url == null && output.web_url == null
    error_message = "Without a domain there are no public URLs."
  }

  assert {
    condition     = output.api_cors_origins == "[]"
    error_message = "Without a website, the API allows no browser origins."
  }
}
