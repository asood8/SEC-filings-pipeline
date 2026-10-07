output "folder_uids" {
  description = "UIDs of the folders, by name."
  value       = { for title, folder in grafana_folder.this : title => folder.uid }
}

output "dashboard_urls" {
  description = "URLs of the provisioned dashboards, by file name."
  value       = { for name, dashboard in grafana_dashboard.this : name => dashboard.url }
}

output "rule_groups" {
  description = "Provisioned alert rule groups, as <folder>/<group>."
  value       = keys(grafana_rule_group.this)
}
