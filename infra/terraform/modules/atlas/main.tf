# MongoDB Atlas: a project with one free M0 cluster for the parsed filings, the application's
# database user, and an IP access list that admits only the VM.
#
# Left to a person: creating the Atlas account and organization, and the organization-level API
# credentials (a service account) that Terraform uses (infra/terraform/README.md, "Accounts and
# credentials").

resource "mongodbatlas_project" "main" {
  name   = var.project_name
  org_id = var.org_id
}

resource "mongodbatlas_advanced_cluster" "main" {
  project_id   = mongodbatlas_project.main.id
  name         = var.cluster_name
  cluster_type = "REPLICASET"

  replication_specs = [
    {
      region_configs = [
        {
          # TENANT + M0 is the free shared tier, run by Atlas on the backing cloud.
          provider_name         = var.provider_name
          backing_provider_name = var.backing_provider_name
          region_name           = var.region_name
          priority              = 7
          electable_specs = {
            instance_size = var.instance_size
          }
        },
      ]
    },
  ]

  lifecycle {
    precondition {
      condition     = var.instance_size == "M0" && var.provider_name == "TENANT"
      error_message = "Refusing to create anything but a free M0 (TENANT) cluster."
    }
  }
}

# Letters and digits only, so the password can go into the connection string unescaped. 32 of
# them is about 190 bits.
resource "random_password" "database_user" {
  length  = 32
  special = false
}

resource "mongodbatlas_database_user" "app" {
  project_id         = mongodbatlas_project.main.id
  username           = var.database_username
  password           = random_password.database_user.result
  auth_database_name = "admin"

  roles {
    role_name     = "readWrite"
    database_name = var.database_name
  }

  # Valid on this cluster only.
  scopes {
    name = mongodbatlas_advanced_cluster.main.name
    type = "CLUSTER"
  }
}

resource "mongodbatlas_project_ip_access_list" "allowed" {
  for_each = var.access_list

  project_id = mongodbatlas_project.main.id
  cidr_block = each.value
  comment    = "Terraform: ${each.key}"
}

locals {
  # mongodb+srv://<cluster host>, without credentials.
  standard_srv = mongodbatlas_advanced_cluster.main.connection_strings.standard_srv

  connection_string = format(
    "%s/?retryWrites=true&w=majority&appName=%s",
    replace(local.standard_srv, "mongodb+srv://", "mongodb+srv://${var.database_username}:${random_password.database_user.result}@"),
    var.cluster_name,
  )
}
