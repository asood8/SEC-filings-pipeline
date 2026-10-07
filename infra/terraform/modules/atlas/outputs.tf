output "project_id" {
  description = "ID of the Atlas project."
  value       = mongodbatlas_project.main.id
}

output "cluster_name" {
  description = "Name of the M0 cluster."
  value       = mongodbatlas_advanced_cluster.main.name
}

output "access_list_cidrs" {
  description = "Addresses allowed to connect to the project's clusters."
  value       = toset([for entry in mongodbatlas_project_ip_access_list.allowed : entry.cidr_block])
}

output "connection_string" {
  description = "Connection string with the application user's credentials (MONGODB_URL in the mongodb Secret)."
  value       = local.connection_string
  sensitive   = true
}
