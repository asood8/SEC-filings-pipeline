# Grafana Cloud Free (brief §9, checked October 2026): 10k active metric series, 50 GB of logs,
# 50 GB of traces, 14-day retention, no credit card. Dashboards, folders, and alert rules cost
# nothing; what counts against the limits is the telemetry the OpenTelemetry Collector sends.

variable "dashboards_dir" {
  description = "Directory of dashboard JSON files (deploy/grafana/dashboards). A missing or empty directory provisions no dashboards."
  type        = string
}

variable "alerting_dir" {
  description = "Directory of alert rule files in Grafana's provisioning format (deploy/grafana/alerting). A missing or empty directory provisions no rules."
  type        = string
}

variable "dashboard_folder" {
  description = "Folder the dashboards go in (the local viewer uses the same name: deploy/grafana/local/dashboards.yaml)."
  type        = string
  default     = "SEC Filings"
}

variable "datasource_uids" {
  description = <<-EOT
    UIDs of the Grafana Cloud stack's metrics, logs, and traces data sources (Connections, Data
    sources, in the stack). The repository's alert rules (and any panel that names a data source
    directly) are written for the local viewer, whose data sources have the UIDs prometheus,
    loki, and tempo; those are rewritten to these. The defaults are Grafana Cloud's usual UIDs;
    check them once.
  EOT
  type = object({
    prometheus = optional(string, "grafanacloud-prom")
    loki       = optional(string, "grafanacloud-logs")
    tempo      = optional(string, "grafanacloud-traces")
  })
  default = {}
}
