# Tests of modules/oci: the free-tier guardrails and the VM's first-boot configuration.
#
# `terraform test` (run by `make tf-check`) plans against a mock provider: no Oracle account,
# no credentials, and nothing is created anywhere.

mock_provider "oci" {
  mock_data "oci_identity_availability_domains" {
    defaults = {
      availability_domains = [
        { name = "Uocm:US-ASHBURN-AD-1" },
        { name = "Uocm:US-ASHBURN-AD-2" },
        { name = "Uocm:US-ASHBURN-AD-3" },
      ]
    }
  }

  mock_data "oci_core_images" {
    defaults = {
      images = [
        {
          id           = "ocid1.image.oc1.iad.mock"
          display_name = "Canonical-Ubuntu-24.04-aarch64-2026.09.30-0"
        },
      ]
    }
  }
}

variables {
  tenancy_ocid     = "ocid1.tenancy.oc1..mock"
  compartment_ocid = "ocid1.tenancy.oc1..mock"
}

run "defaults_use_the_whole_always_free_allowance_and_nothing_more" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  assert {
    condition     = oci_core_instance.k3s.shape == "VM.Standard.A1.Flex"
    error_message = "The VM must use the Always Free Ampere A1 shape."
  }

  assert {
    condition     = oci_core_instance.k3s.shape_config[0].ocpus == 2 && oci_core_instance.k3s.shape_config[0].memory_in_gbs == 12
    error_message = "The VM should get the whole allowance: 2 OCPU and 12 GB."
  }

  assert {
    condition     = oci_core_instance.k3s.source_details[0].boot_volume_size_in_gbs + oci_core_volume.data.size_in_gbs <= 200
    error_message = "Boot and data volumes must fit in 200 GB."
  }

  assert {
    condition     = oci_core_instance.k3s.availability_domain == "Uocm:US-ASHBURN-AD-1"
    error_message = "availability_domain_number 1 should pick the first availability domain."
  }

  assert {
    condition     = oci_core_volume.data.availability_domain == oci_core_instance.k3s.availability_domain
    error_message = "The block volume must be in the VM's availability domain to attach."
  }

  assert {
    condition     = oci_core_instance.k3s.source_details[0].source_id == "ocid1.image.oc1.iad.mock"
    error_message = "The VM should boot the newest Ubuntu aarch64 image."
  }
}

run "inbound_traffic_is_closed_by_default" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  assert {
    condition     = length(oci_core_security_list.k3s.ingress_security_rules) == 0
    error_message = "With ssh_ingress_cidr unset, the security list must admit nothing."
  }

  assert {
    condition     = length(oci_core_default_security_list.default.ingress_security_rules) == 0
    error_message = "The VCN's default security list (SSH from anywhere) must be emptied."
  }

  assert {
    condition     = length(oci_core_subnet.public.security_list_ids) == 1
    error_message = "The subnet must use one security list, the module's own (not the VCN's default)."
  }

  assert {
    condition     = !contains(keys(oci_core_instance.k3s.metadata), "ssh_authorized_keys")
    error_message = "No SSH key is installed unless one is given."
  }
}

run "ssh_opens_only_port_22_from_the_given_range" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    ssh_ingress_cidr    = "203.0.113.7/32"
    ssh_authorized_keys = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAImock test@example"
  }

  assert {
    condition     = length(oci_core_security_list.k3s.ingress_security_rules) == 1
    error_message = "Exactly one inbound rule is expected."
  }

  assert {
    condition = alltrue([
      for rule in oci_core_security_list.k3s.ingress_security_rules :
      rule.source == "203.0.113.7/32" && rule.protocol == "6" && rule.tcp_options[0].min == 22 && rule.tcp_options[0].max == 22
    ])
    error_message = "The inbound rule must be TCP 22 from ssh_ingress_cidr only."
  }

  assert {
    condition     = oci_core_instance.k3s.metadata.ssh_authorized_keys == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAImock test@example"
    error_message = "The SSH key should be passed to the VM."
  }
}

run "cloud_init_is_valid_yaml_and_bootstraps_k3s_and_argo_cd" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  assert {
    condition     = startswith(base64decode(oci_core_instance.k3s.metadata.user_data), "#cloud-config\n")
    error_message = "User data must start with #cloud-config."
  }

  assert {
    condition     = can(yamldecode(base64decode(oci_core_instance.k3s.metadata.user_data)))
    error_message = "User data must be valid YAML."
  }

  assert {
    condition = anytrue([
      for f in yamldecode(base64decode(oci_core_instance.k3s.metadata.user_data)).write_files :
      f.path == "/etc/rancher/k3s/config.yaml" && strcontains(f.content, "- traefik") && strcontains(f.content, "default-local-storage-path: /mnt/k3s-data/local-path")
    ])
    error_message = "k3s must be configured with Traefik disabled and volumes on the block volume."
  }

  assert {
    condition = anytrue([
      for f in yamldecode(base64decode(oci_core_instance.k3s.metadata.user_data)).write_files :
      f.path == "/etc/filings-bootstrap.env" && strcontains(f.content, "K3S_VERSION='v1.36.5+k3s1'") && strcontains(f.content, "ARGOCD_INSTALL_PATH='deploy/argocd/install'") && strcontains(f.content, "ARGOCD_APPS_PATH='deploy/argocd'")
    ])
    error_message = "The bootstrap settings must pin k3s and point at the Argo CD manifests."
  }

  assert {
    # The script is embedded intact: its first and last lines survive the YAML indentation.
    condition = anytrue([
      for f in yamldecode(base64decode(oci_core_instance.k3s.metadata.user_data)).write_files :
      f.path == "/usr/local/sbin/filings-bootstrap" && startswith(f.content, "#!/usr/bin/env bash\n") && strcontains(f.content, "kubectl apply -k \"$src/$ARGOCD_APPS_PATH\"")
    ])
    error_message = "The bootstrap script must be embedded intact."
  }

  assert {
    condition     = length(base64encode(local.cloud_init)) <= 32000
    error_message = "OCI limits user data to 32,000 bytes (base64-encoded)."
  }
}

run "the_metadata_service_requires_v2" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  assert {
    condition     = oci_core_instance.k3s.instance_options[0].are_legacy_imds_endpoints_disabled == true
    error_message = "Legacy (v1) metadata endpoints must be disabled."
  }
}

# --- Paid configurations are refused ---------------------------------------------------------

run "refuses_a_shape_other_than_a1_flex" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    instance_shape = "VM.Standard.E5.Flex"
  }

  expect_failures = [var.instance_shape]
}

run "refuses_more_than_2_ocpus" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    instance_ocpus = 4
  }

  expect_failures = [var.instance_ocpus]
}

run "refuses_more_than_12_gb_of_memory" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    instance_memory_gbs = 24
  }

  expect_failures = [var.instance_memory_gbs]
}

run "refuses_volumes_beyond_200_gb_in_total" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    boot_volume_size_gbs = 100
    data_volume_size_gbs = 150
  }

  expect_failures = [oci_core_instance.k3s]
}

run "refuses_volumes_beyond_the_remaining_allowance" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    free_block_storage_gbs = 150
    data_volume_size_gbs   = 101
  }

  expect_failures = [oci_core_instance.k3s]
}

run "refuses_higher_performance_block_storage" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    data_volume_vpus_per_gb = 20
  }

  expect_failures = [var.data_volume_vpus_per_gb]
}

run "refuses_ssh_from_the_whole_internet" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    ssh_ingress_cidr = "0.0.0.0/0"
  }

  expect_failures = [var.ssh_ingress_cidr]
}

run "refuses_ssh_from_a_wide_range" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    ssh_ingress_cidr = "10.0.0.0/8"
  }

  expect_failures = [var.ssh_ingress_cidr]
}

run "refuses_a_missing_availability_domain" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    availability_domain_number = 3
  }

  override_data {
    target = data.oci_identity_availability_domains.all
    values = {
      availability_domains = [{ name = "Uocm:EU-AMSTERDAM-1-AD-1" }]
    }
  }

  expect_failures = [oci_core_instance.k3s, oci_core_volume.data]
}

run "refuses_a_git_url_with_credentials" {
  command = plan

  module {
    source = "../../modules/oci"
  }

  variables {
    git_repo_url = "https://user:token@github.com/asood8/SEC-filings-pipeline.git"
  }

  expect_failures = [var.git_repo_url]
}
