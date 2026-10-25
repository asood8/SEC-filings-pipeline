# Production: everything outside the Kubernetes cluster, for $0 a month (brief §9).
#
#   module.oci         Oracle Cloud: network + one Ampere A1 VM that boots into k3s and Argo CD
#   module.cloudflare  Tunnel to the API, DNS, R2 buckets, Pages project for the website
#   module.atlas       MongoDB Atlas: M0 cluster, database user, access list for the VM
#   module.grafana     Grafana Cloud: dashboards and alert rules from deploy/grafana
#
# What runs inside the cluster is not here: Argo CD deploys it from deploy/k8s (ADR-0017).
# The order of a first apply, and the manual steps before it, are in README.md.

locals {
  repo_root = "${path.root}/../../../.."
}

module "oci" {
  source = "../../modules/oci"

  tenancy_ocid               = var.oci_tenancy_ocid
  compartment_ocid           = coalesce(var.oci_compartment_ocid, var.oci_tenancy_ocid)
  availability_domain_number = var.oci_availability_domain_number
  instance_ocpus             = var.oci_instance_ocpus
  instance_memory_gbs        = var.oci_instance_memory_gbs
  data_volume_size_gbs       = var.data_volume_size_gbs
  ssh_ingress_cidr           = var.ssh_ingress_cidr
  ssh_authorized_keys        = var.ssh_authorized_keys
  k3s_version                = var.k3s_version
  git_revision               = var.git_revision

  freeform_tags = {
    project    = "sec-filings-pipeline"
    managed-by = "terraform"
  }
}

module "cloudflare" {
  source = "../../modules/cloudflare"

  account_id               = var.cloudflare_account_id
  zone_name                = var.domain
  api_subdomain            = var.api_subdomain
  web_subdomain            = var.web_subdomain
  r2_location              = var.r2_location
  backup_retention_days    = var.backup_retention_days
  pages_github_integration = var.pages_github_integration
  google_client_id         = var.google_client_id
  # The same Node.js as CI and local development.
  node_version = trimspace(file("${local.repo_root}/web/.nvmrc"))
}

module "atlas" {
  source = "../../modules/atlas"

  org_id                = var.atlas_org_id
  backing_provider_name = var.atlas_backing_provider
  region_name           = var.atlas_region

  # Only the VM (its outbound traffic leaves from its public IP), plus any extras.
  access_list = merge(var.atlas_extra_access, {
    k3s-vm = "${module.oci.public_ip}/32"
  })
}

module "grafana" {
  source = "../../modules/grafana"

  dashboards_dir  = "${local.repo_root}/deploy/grafana/dashboards"
  alerting_dir    = "${local.repo_root}/deploy/grafana/alerting"
  datasource_uids = var.grafana_datasource_uids
}
