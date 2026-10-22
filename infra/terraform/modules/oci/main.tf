# The production VM on Oracle Cloud's Always Free tier: one Ampere A1 (arm64) instance running a
# single-node k3s cluster, in its own network with every inbound port closed.
#
# Every resource here is Always Free (brief §9, checked October 2026):
# - VCN, subnet, internet gateway, route table, security lists: networking objects are free.
#   There is deliberately no NAT gateway, service gateway, load balancer, or reserved public IP:
#   the VM's ephemeral public IP and the internet gateway cover outbound traffic, and Cloudflare
#   Tunnel needs nothing inbound.
# - The A1 instance and its boot volume: within 2 OCPU / 12 GB and the 200 GB block storage
#   allowance (validated in variables.tf, and the total is checked below).
# - One Balanced block volume, no volume backups (Always Free has only five; backups go to R2).
#
# Two Always Free rules to know:
# - Idle reclamation: Oracle may reclaim an Always Free instance whose CPU (95th percentile),
#   network, and memory use all stay under 20% for 7 days. This stack holds ~9 of 12 GB of memory
#   (brief §7), well above that, but a stopped or empty VM would qualify.
# - Capacity: A1 hosts are often full, and creating the instance then fails with "Out of host
#   capacity". Nothing is wrong with the configuration: re-run the apply later, or set
#   availability_domain_number to another domain in regions that have several.

locals {
  # The block volume's mount point; the bootstrap script and the k3s settings use it.
  data_mount = "/mnt/k3s-data"

  availability_domains = data.oci_identity_availability_domains.all.availability_domains
  availability_domain  = try(local.availability_domains[var.availability_domain_number - 1].name, null)

  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    data_mount          = local.data_mount
    k3s_version         = var.k3s_version
    git_repo_url        = var.git_repo_url
    git_revision        = var.git_revision
    argocd_install_path = var.argocd_install_path
    argocd_apps_path    = var.argocd_apps_path
    bootstrap_script    = chomp(file("${path.module}/bootstrap.sh"))
  })
}

data "oci_identity_availability_domains" "all" {
  compartment_id = var.tenancy_ocid
}

# The newest Canonical Ubuntu platform image for arm64 that runs on the A1 shape. The regular
# image, not "Minimal": cloud-init, iptables-persistent, and the usual tools are expected.
data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = var.ubuntu_version
  shape                    = var.instance_shape
  state                    = "AVAILABLE"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"

  filter {
    name   = "display_name"
    values = ["^Canonical-Ubuntu-${replace(var.ubuntu_version, ".", "\\.")}-aarch64-"]
    regex  = true
  }
}

# --- Network ----------------------------------------------------------------------------------

resource "oci_core_vcn" "main" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.name_prefix}-vcn"
  cidr_blocks    = [var.vcn_cidr]
  dns_label      = var.name_prefix
  freeform_tags  = var.freeform_tags
}

# Every VCN comes with a default security list that admits SSH from anywhere. The subnet below
# doesn't use it, and this empties it, so attaching it by mistake can't open anything.
resource "oci_core_default_security_list" "default" {
  manage_default_resource_id = oci_core_vcn.main.default_security_list_id
  display_name               = "${var.name_prefix}-default-empty"
  freeform_tags              = var.freeform_tags
}

resource "oci_core_internet_gateway" "main" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.name_prefix}-igw"
  enabled        = true
  freeform_tags  = var.freeform_tags
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.name_prefix}-public"
  freeform_tags  = var.freeform_tags

  route_rules {
    description       = "Everything outside the VCN goes through the internet gateway."
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.main.id
  }
}

# Outbound: anything (SEC, Cloudflare, Atlas, Grafana Cloud, Gemini, GHCR, GitHub). The rules are
# stateful, so replies to outbound connections come back in.
# Inbound: nothing, unless ssh_ingress_cidr is set. Oracle's default list also admits ICMP
# "fragmentation needed" messages for path MTU discovery; they are left out to keep inbound fully
# closed. Add that rule if large downloads ever stall.
resource "oci_core_security_list" "k3s" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.name_prefix}-k3s"
  freeform_tags  = var.freeform_tags

  egress_security_rules {
    description      = "All outbound traffic."
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "all"
    stateless        = false
  }

  dynamic "ingress_security_rules" {
    for_each = var.ssh_ingress_cidr == null ? [] : [var.ssh_ingress_cidr]

    content {
      description = "SSH for maintenance, from ssh_ingress_cidr only."
      protocol    = "6" # TCP
      source      = ingress_security_rules.value
      source_type = "CIDR_BLOCK"
      stateless   = false

      tcp_options {
        min = 22
        max = 22
      }
    }
  }
}

resource "oci_core_subnet" "public" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.main.id
  display_name               = "${var.name_prefix}-public"
  cidr_block                 = var.subnet_cidr
  dns_label                  = "public"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.k3s.id]
  prohibit_public_ip_on_vnic = false
  freeform_tags              = var.freeform_tags
}

# --- The VM -----------------------------------------------------------------------------------

resource "oci_core_instance" "k3s" {
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  display_name        = "${var.name_prefix}-k3s"
  shape               = var.instance_shape
  freeform_tags       = var.freeform_tags

  shape_config {
    ocpus         = var.instance_ocpus
    memory_in_gbs = var.instance_memory_gbs
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu.images[0].id
    boot_volume_size_in_gbs = var.boot_volume_size_gbs
    boot_volume_vpus_per_gb = 10 # Balanced, the Always Free performance level
  }

  create_vnic_details {
    subnet_id = oci_core_subnet.public.id
    # An ephemeral public IP (free): it stays with the VM across stops and restarts, and is
    # released only when the VM is terminated. MongoDB Atlas allows connections from it.
    assign_public_ip = true
    hostname_label   = "${var.name_prefix}-k3s"
  }

  metadata = merge(
    { user_data = base64encode(local.cloud_init) },
    var.ssh_authorized_keys == "" ? {} : { ssh_authorized_keys = var.ssh_authorized_keys },
  )

  # Only the metadata service's v2 endpoints, which require a header that a request forged
  # through a server-side request forgery bug wouldn't carry.
  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  # Oracle judges idleness by CPU, network, and memory, and sees memory only through the Oracle
  # Cloud Agent's monitoring plugin. Memory is what keeps this VM above the 20% line (the stack
  # holds about half of it; CPU is idle between 8-Ks), so the plugin must be on.
  agent_config {
    is_monitoring_disabled = false
    plugins_config {
      name          = "Compute Instance Monitoring"
      desired_state = "ENABLED"
    }
  }

  # Restart the VM after an infrastructure failure.
  availability_config {
    recovery_action = "RESTORE_INSTANCE"
  }

  # Delete the boot volume with the VM: an orphaned boot volume would keep counting toward the
  # 200 GB allowance.
  preserve_boot_volume = false

  lifecycle {
    # A newer Ubuntu image or an edited cloud-init would otherwise replace the VM: k3s state and
    # the public IP would be lost, and A1 capacity may not be available to create it again.
    # cloud-init runs only on the first boot anyway. Rebuild deliberately with
    #   terraform apply -replace=module.oci.oci_core_instance.k3s
    ignore_changes = [source_details[0].source_id, metadata]

    precondition {
      condition     = local.availability_domain != null
      error_message = "availability_domain_number ${var.availability_domain_number} doesn't exist: this region has ${length(local.availability_domains)} availability domain(s)."
    }

    precondition {
      condition     = length(data.oci_core_images.ubuntu.images) > 0
      error_message = "No Canonical Ubuntu ${var.ubuntu_version} aarch64 image found for ${var.instance_shape} in this region."
    }

    precondition {
      condition     = var.boot_volume_size_gbs + var.data_volume_size_gbs <= var.free_block_storage_gbs
      error_message = "Boot volume (${var.boot_volume_size_gbs} GB) plus data volume (${var.data_volume_size_gbs} GB) exceeds the ${var.free_block_storage_gbs} GB of Always Free block storage."
    }
  }
}

# --- Storage for k3s --------------------------------------------------------------------------

# k3s's local-path provisioner puts every persistent volume here (cloud-init mounts it at
# /mnt/k3s-data). A separate volume outlives the VM: if the VM is rebuilt, attach it again and
# the data is still there.
resource "oci_core_volume" "data" {
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  display_name        = "${var.name_prefix}-k3s-data"
  size_in_gbs         = var.data_volume_size_gbs
  vpus_per_gb         = var.data_volume_vpus_per_gb
  freeform_tags       = var.freeform_tags

  lifecycle {
    precondition {
      condition     = local.availability_domain != null
      error_message = "availability_domain_number ${var.availability_domain_number} doesn't exist: this region has ${length(local.availability_domains)} availability domain(s)."
    }
  }
}

# Paravirtualized: the volume appears as a plain disk, with no iSCSI login commands to run.
resource "oci_core_volume_attachment" "data" {
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.k3s.id
  volume_id       = oci_core_volume.data.id
  display_name    = "${var.name_prefix}-k3s-data"
  is_read_only    = false
  is_shareable    = false
}
