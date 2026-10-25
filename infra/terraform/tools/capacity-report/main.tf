# Asks Oracle whether it has A1 capacity for a VM size in one availability domain, without
# creating or changing anything: a compute capacity report, which only answers. Growing the VM
# from 1 OCPU and 6 GB to 2 and 12 reboots it even when the resize then fails for capacity, so
# scripts/retry-vm-capacity.sh resizes only after a report says AVAILABLE (docs/adr/0020). It
# applies this configuration with a throwaway local state; nothing here touches production's.

terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 9.8, < 10.0"
    }
  }
}

variable "region" {
  description = "The tenancy's home region, as in production.tfvars."
  type        = string
}

variable "config_file_profile" {
  description = "Profile in ~/.oci/config, as in production.tfvars."
  type        = string
  default     = "DEFAULT"
}

variable "compartment_ocid" {
  description = "The VM's compartment."
  type        = string
}

variable "availability_domain" {
  description = "The availability domain's full name, such as Uocm:US-ASHBURN-AD-1."
  type        = string
}

variable "ocpus" {
  description = "OCPUs of the size to ask about."
  type        = number
}

variable "memory_gbs" {
  description = "Memory in GB of the size to ask about."
  type        = number
}

provider "oci" {
  auth                = "APIKey"
  config_file_profile = var.config_file_profile
  region              = var.region
}

resource "oci_core_compute_capacity_report" "a1" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.availability_domain

  shape_availabilities {
    instance_shape = "VM.Standard.A1.Flex"

    instance_shape_config {
      ocpus         = var.ocpus
      memory_in_gbs = var.memory_gbs
    }
  }
}

output "availability" {
  description = "AVAILABLE, OUT_OF_HOST_CAPACITY, or HARDWARE_NOT_SUPPORTED."
  value       = oci_core_compute_capacity_report.a1.shape_availabilities[0].availability_status
}
