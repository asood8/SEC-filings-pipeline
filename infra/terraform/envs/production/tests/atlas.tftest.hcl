# Tests of modules/atlas against a mock provider (no account, no credentials, nothing created):
# the cluster is locked to the free M0 tier, and the access list admits only given addresses.

mock_provider "mongodbatlas" {
  mock_resource "mongodbatlas_advanced_cluster" {
    defaults = {
      connection_strings = {
        standard_srv = "mongodb+srv://filings.mock.mongodb.net"
      }
    }
  }
}

# hashicorp/random runs locally (it calls no API), so the real provider is used.

variables {
  org_id      = "0123456789abcdef01234567"
  access_list = { k3s-vm = "203.0.113.7/32" }
}

run "creates_a_free_m0_cluster" {
  command = plan

  module {
    source = "../../modules/atlas"
  }

  assert {
    condition = alltrue([
      for spec in mongodbatlas_advanced_cluster.main.replication_specs : alltrue([
        for region in spec.region_configs :
        region.provider_name == "TENANT" && region.electable_specs.instance_size == "M0"
      ])
    ])
    error_message = "The cluster must be M0 on the TENANT provider: the free tier."
  }

  assert {
    condition     = mongodbatlas_project_ip_access_list.allowed["k3s-vm"].cidr_block == "203.0.113.7/32"
    error_message = "The VM's address must be on the access list."
  }

  assert {
    condition     = length(mongodbatlas_project_ip_access_list.allowed) == 1
    error_message = "Only the given addresses may be on the access list."
  }

  assert {
    condition = (
      one(mongodbatlas_database_user.app.roles).role_name == "readWrite" &&
      one(mongodbatlas_database_user.app.roles).database_name == "filings"
    )
    error_message = "The application user gets readWrite on its own database only."
  }
}

run "the_connection_string_carries_the_user_and_password" {
  command = apply # against the mock provider: the password and host only exist after "creation"

  module {
    source = "../../modules/atlas"
  }

  assert {
    condition     = startswith(output.connection_string, "mongodb+srv://filings:${random_password.database_user.result}@filings.mock.mongodb.net/?")
    error_message = "The connection string must embed the user's credentials before the cluster host."
  }

  assert {
    condition     = strcontains(output.connection_string, "retryWrites=true") && strcontains(output.connection_string, "w=majority")
    error_message = "The connection string should enable retryable writes and majority write concern."
  }

  assert {
    condition     = length(random_password.database_user.result) == 32 && can(regex("^[A-Za-z0-9]+$", random_password.database_user.result))
    error_message = "The password must be 32 letters and digits, safe in a URL without escaping."
  }
}

# --- Paid configurations are refused ---------------------------------------------------------

run "refuses_a_billed_tier" {
  command = plan

  module {
    source = "../../modules/atlas"
  }

  variables {
    instance_size = "M10"
  }

  expect_failures = [var.instance_size]
}

run "refuses_a_dedicated_cluster_provider" {
  command = plan

  module {
    source = "../../modules/atlas"
  }

  variables {
    provider_name = "AWS"
  }

  expect_failures = [var.provider_name]
}

run "refuses_access_from_anywhere" {
  command = plan

  module {
    source = "../../modules/atlas"
  }

  variables {
    access_list = { k3s-vm = "203.0.113.7/32", everyone = "0.0.0.0/0" }
  }

  expect_failures = [var.access_list]
}
