# Oracle Cloud Always Free limits (brief §9, checked October 2026; re-verify before applying):
# - Ampere A1: 2 OCPU and 12 GB of memory in total per tenancy (halved in June 2026).
# - Block storage: 200 GB in total, boot volumes included, at the default (Balanced) performance.
# - Always Free resources exist only in the tenancy's home region.
# The validations below refuse anything beyond those limits, so a typo can't create a paid
# resource. The account itself must stay on the Free tier (never upgraded to pay-as-you-go): an
# upgraded account would bill whatever exceeds the allowance instead of refusing it.

variable "tenancy_ocid" {
  description = "OCID of the tenancy (availability domains are listed per tenancy)."
  type        = string

  validation {
    condition     = can(regex("^ocid1\\.tenancy\\.", var.tenancy_ocid))
    error_message = "tenancy_ocid must be a tenancy OCID (ocid1.tenancy...)."
  }
}

variable "compartment_ocid" {
  description = "OCID of the compartment to create everything in (the tenancy OCID means the root compartment)."
  type        = string

  validation {
    condition     = can(regex("^ocid1\\.(tenancy|compartment)\\.", var.compartment_ocid))
    error_message = "compartment_ocid must be a compartment or tenancy OCID."
  }
}

variable "name_prefix" {
  description = "Prefix for display names and DNS labels."
  type        = string
  default     = "filings"

  validation {
    # It becomes part of DNS labels, which allow letters and digits only, starting with a letter.
    condition     = can(regex("^[a-z][a-z0-9]{0,9}$", var.name_prefix))
    error_message = "name_prefix must be 1-10 lowercase letters or digits, starting with a letter."
  }
}

variable "availability_domain_number" {
  description = <<-EOT
    Which availability domain (1-3) gets the VM and its block volume. A1 capacity is often
    exhausted ("Out of host capacity"); if an apply fails that way, retry later or try another
    domain in regions that have more than one.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = contains([1, 2, 3], var.availability_domain_number)
    error_message = "availability_domain_number must be 1, 2, or 3."
  }
}

variable "instance_shape" {
  description = "Compute shape. Only the Always Free Ampere A1 flexible shape is allowed."
  type        = string
  default     = "VM.Standard.A1.Flex"

  validation {
    condition     = var.instance_shape == "VM.Standard.A1.Flex"
    error_message = "Only VM.Standard.A1.Flex is Always Free. Any other shape costs money."
  }
}

variable "instance_ocpus" {
  description = "OCPUs for the VM (Always Free: 2 in total for all A1 instances)."
  type        = number
  default     = 2

  validation {
    condition     = var.instance_ocpus >= 1 && var.instance_ocpus <= 2 && floor(var.instance_ocpus) == var.instance_ocpus
    error_message = "instance_ocpus must be 1 or 2: the Always Free A1 allowance is 2 OCPU."
  }
}

variable "instance_memory_gbs" {
  description = "Memory for the VM in GB (Always Free: 12 GB in total for all A1 instances)."
  type        = number
  default     = 12

  validation {
    condition     = var.instance_memory_gbs >= 1 && var.instance_memory_gbs <= 12 && floor(var.instance_memory_gbs) == var.instance_memory_gbs
    error_message = "instance_memory_gbs must be a whole number from 1 to 12: the Always Free A1 allowance is 12 GB."
  }
}

variable "boot_volume_size_gbs" {
  description = "Boot volume size in GB (OCI minimum 50). Counts toward the 200 GB Always Free block storage."
  type        = number
  default     = 50

  validation {
    condition     = var.boot_volume_size_gbs >= 50 && var.boot_volume_size_gbs <= 150
    error_message = "boot_volume_size_gbs must be 50-150 (OCI minimum 50; the data volume needs at least 50 of the 200 GB free)."
  }
}

variable "data_volume_size_gbs" {
  description = "Block volume for k3s persistent volumes (Postgres, Redpanda, models), in GB (OCI minimum 50)."
  type        = number
  default     = 100

  validation {
    condition     = var.data_volume_size_gbs >= 50 && var.data_volume_size_gbs <= 150
    error_message = "data_volume_size_gbs must be 50-150 (OCI minimum 50; the boot volume needs at least 50 of the 200 GB free)."
  }
}

variable "data_volume_vpus_per_gb" {
  description = "Block volume performance. Always Free covers the default Balanced level (10 VPUs/GB) only."
  type        = number
  default     = 10

  validation {
    condition     = var.data_volume_vpus_per_gb == 10
    error_message = "Only 10 VPUs/GB (Balanced) is covered by Always Free; Higher and Ultra High Performance cost money."
  }
}

variable "free_block_storage_gbs" {
  description = <<-EOT
    The Always Free block storage allowance still available to this configuration, in GB. Lower
    it if the tenancy has other volumes (for example another Always Free VM's boot volume).
  EOT
  type        = number
  default     = 200

  validation {
    condition     = var.free_block_storage_gbs >= 100 && var.free_block_storage_gbs <= 200
    error_message = "free_block_storage_gbs must be 100-200: the Always Free allowance is 200 GB."
  }
}

variable "ubuntu_version" {
  description = "Ubuntu LTS release for the VM's platform image (the newest aarch64 build is looked up)."
  type        = string
  default     = "24.04"

  validation {
    condition     = can(regex("^[0-9]{2}\\.04$", var.ubuntu_version))
    error_message = "ubuntu_version must be an LTS release such as 24.04."
  }
}

variable "vcn_cidr" {
  description = "Address range of the VCN. Must not overlap k3s's pod (10.42.0.0/16) and service (10.43.0.0/16) ranges."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vcn_cidr, 0))
    error_message = "vcn_cidr must be a valid IPv4 CIDR block."
  }
}

variable "subnet_cidr" {
  description = "Address range of the public subnet, inside vcn_cidr."
  type        = string
  default     = "10.0.0.0/24"

  validation {
    condition     = can(cidrhost(var.subnet_cidr, 0))
    error_message = "subnet_cidr must be a valid IPv4 CIDR block."
  }
}

variable "ssh_ingress_cidr" {
  description = <<-EOT
    Source range allowed to reach SSH (port 22), such as your own address as x.x.x.x/32. null
    (the default) keeps every inbound port closed: Cloudflare Tunnel only makes outbound
    connections. Open it temporarily for maintenance, then close it again.
  EOT
  type        = string
  default     = null

  validation {
    # An IPv4 block of at most 65,536 addresses (a /16 or narrower): an ISP's range is fine, a
    # large slice of the internet is not.
    condition = var.ssh_ingress_cidr == null || (
      can(cidrnetmask(coalesce(var.ssh_ingress_cidr, "0.0.0.0/0"))) &&
      try(tonumber(split("/", coalesce(var.ssh_ingress_cidr, "0.0.0.0/0"))[1]) >= 16, false)
    )
    error_message = "ssh_ingress_cidr must be null or an IPv4 CIDR block of /16 or narrower, such as 203.0.113.7/32; SSH open to the internet is refused."
  }
}

variable "ssh_authorized_keys" {
  description = "Public SSH key(s) for the default ubuntu user, one per line. Empty means no key (no SSH login at all)."
  type        = string
  default     = ""
}

variable "k3s_version" {
  description = "k3s release to install (a tag from github.com/k3s-io/k3s/releases). Pinned so a rebuild installs the same Kubernetes."
  type        = string
  default     = "v1.36.5+k3s1"

  validation {
    condition     = can(regex("^v1\\.[0-9]+\\.[0-9]+\\+k3s[0-9]+$", var.k3s_version))
    error_message = "k3s_version must be a k3s release tag such as v1.36.5+k3s1."
  }
}

variable "git_repo_url" {
  description = "Git repository cloud-init clones to bootstrap Argo CD (public, so no credentials are needed)."
  type        = string
  default     = "https://github.com/asood8/SEC-filings-pipeline.git"

  validation {
    # No "@": credentials in the URL would be readable from the instance metadata service.
    condition     = can(regex("^https://[A-Za-z0-9._~:/%+-]+$", var.git_repo_url))
    error_message = "git_repo_url must be an https:// URL without credentials (cloud-init clones it anonymously)."
  }
}

variable "git_revision" {
  description = "Branch, tag, or commit of git_repo_url whose Argo CD manifests cloud-init applies."
  type        = string
  default     = "main"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.git_revision))
    error_message = "git_revision may contain only letters, digits, '.', '_', '/', and '-'."
  }
}

variable "argocd_install_path" {
  description = "Directory in the repository holding Argo CD's own install (a Kustomization that pins its version)."
  type        = string
  default     = "deploy/argocd/install"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", var.argocd_install_path)) && !strcontains(var.argocd_install_path, "..")
    error_message = "argocd_install_path must be a relative path inside the repository."
  }
}

variable "argocd_apps_path" {
  description = "Directory in the repository holding the Argo CD project and Applications, applied after the install."
  type        = string
  default     = "deploy/argocd"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", var.argocd_apps_path)) && !strcontains(var.argocd_apps_path, "..")
    error_message = "argocd_apps_path must be a relative path inside the repository."
  }
}

variable "freeform_tags" {
  description = "Tags added to every resource."
  type        = map(string)
  default     = {}
}
