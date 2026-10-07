output "instance_id" {
  description = "OCID of the k3s VM."
  value       = oci_core_instance.k3s.id
}

output "public_ip" {
  description = "The VM's public IP: the source address of its outbound traffic (Atlas allows it). Nothing listens on it unless SSH is opened."
  value       = oci_core_instance.k3s.public_ip
}

output "private_ip" {
  description = "The VM's address inside the VCN."
  value       = oci_core_instance.k3s.private_ip
}

output "availability_domain" {
  description = "Availability domain of the VM and its block volume."
  value       = oci_core_instance.k3s.availability_domain
}

output "image_name" {
  description = "Ubuntu image the VM was created from (newer images don't replace it)."
  value       = data.oci_core_images.ubuntu.images[0].display_name
}

output "data_volume_id" {
  description = "OCID of the block volume holding k3s's persistent volumes."
  value       = oci_core_volume.data.id
}

output "vcn_id" {
  description = "OCID of the VCN."
  value       = oci_core_vcn.main.id
}
