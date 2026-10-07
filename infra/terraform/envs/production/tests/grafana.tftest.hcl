# Tests of modules/grafana against a mock provider (no stack, no token, nothing created): the
# dashboards and alert rules are read from files in Grafana's own formats and translated for
# Grafana Cloud. The fixtures in tests/fixtures/grafana have the shape of deploy/grafana.

mock_provider "grafana" {}

variables {
  dashboards_dir = "./tests/fixtures/grafana/dashboards"
  alerting_dir   = "./tests/fixtures/grafana/alerting"
}

run "provisions_every_dashboard_into_the_shared_folder" {
  command = plan

  module {
    source = "../../modules/grafana"
  }

  assert {
    condition     = keys(grafana_dashboard.this) == ["sample"]
    error_message = "Each dashboards/*.json file becomes one dashboard, keyed by file name."
  }

  assert {
    # The same folder UID as the local viewer's provisioning (deploy/grafana/local).
    condition     = grafana_folder.this["SEC Filings"].uid == "sec-filings"
    error_message = "The folder UID is derived from its title."
  }

  assert {
    condition     = grafana_dashboard.this["sample"].overwrite
    error_message = "The files win over edits made in the UI."
  }
}

run "dashboards_keep_template_variables_and_get_the_stacks_data_source" {
  command = plan

  module {
    source = "../../modules/grafana"
  }

  assert {
    condition     = jsondecode(grafana_dashboard.this["sample"].config_json).panels[0].datasource.uid == "$${datasource}"
    error_message = "A panel using the template variable must be left alone."
  }

  assert {
    condition     = jsondecode(grafana_dashboard.this["sample"].config_json).panels[1].datasource.uid == "grafanacloud-prom"
    error_message = "A panel naming the local viewer's Prometheus must point at the stack's."
  }
}

run "translates_alert_rules_from_the_provisioning_format" {
  command = plan

  module {
    source = "../../modules/grafana"
  }

  assert {
    condition     = toset(keys(grafana_rule_group.this)) == toset(["SEC Filings/pipeline", "Operations/slow-checks"])
    error_message = "Each group in each file becomes one rule group, keyed by folder and name."
  }

  assert {
    condition     = grafana_rule_group.this["SEC Filings/pipeline"].interval_seconds == 60 && grafana_rule_group.this["Operations/slow-checks"].interval_seconds == 300
    error_message = "Group intervals such as 1m and 5m must become seconds."
  }

  assert {
    condition     = grafana_rule_group.this["SEC Filings/pipeline"].rule[0].data[0].datasource_uid == "grafanacloud-prom"
    error_message = "The local viewer's Prometheus must be replaced by the stack's."
  }

  assert {
    condition     = grafana_rule_group.this["SEC Filings/pipeline"].rule[0].data[1].datasource_uid == "__expr__"
    error_message = "Expressions (__expr__) are not a data source and must stay as they are."
  }

  assert {
    condition = (
      grafana_rule_group.this["SEC Filings/pipeline"].rule[0].annotations["__dashboardUid__"] == "sample-dashboard" &&
      grafana_rule_group.this["SEC Filings/pipeline"].rule[0].annotations["__panelId__"] == "2" &&
      startswith(grafana_rule_group.this["SEC Filings/pipeline"].rule[0].annotations["summary"], "{{ .Labels.")
    )
    error_message = "dashboardUid and panelId must become the panel-link annotations, beside the file's own."
  }

  assert {
    condition     = !contains(keys(grafana_rule_group.this["Operations/slow-checks"].rule[0].annotations), "__dashboardUid__")
    error_message = "A dashboardUid without a panelId must be dropped: the API needs both or neither."
  }

  assert {
    condition     = jsondecode(grafana_rule_group.this["SEC Filings/pipeline"].rule[0].data[0].model).expr == "max by (messaging_destination_name) (filings_dlq_depth)"
    error_message = "The query model must be passed through unchanged."
  }

  assert {
    condition = (
      grafana_rule_group.this["SEC Filings/pipeline"].rule[0].no_data_state == "OK" &&
      grafana_rule_group.this["Operations/slow-checks"].rule[0].no_data_state == "NoData" &&
      grafana_rule_group.this["Operations/slow-checks"].rule[0].for == "10m"
    )
    error_message = "Rule settings must come from the file, with Grafana's defaults otherwise."
  }

  assert {
    condition     = grafana_rule_group.this["Operations/slow-checks"].rule[0].notification_settings[0].contact_point == "discord"
    error_message = "A rule's notification_settings.receiver must become its contact point."
  }

  assert {
    condition     = length(grafana_rule_group.this["SEC Filings/pipeline"].rule[0].notification_settings) == 0
    error_message = "A rule without notification settings follows the stack's notification policy."
  }
}

run "uses_the_data_source_uids_given" {
  command = plan

  module {
    source = "../../modules/grafana"
  }

  variables {
    datasource_uids = { prometheus = "my-mimir" }
  }

  assert {
    condition     = grafana_rule_group.this["SEC Filings/pipeline"].rule[0].data[0].datasource_uid == "my-mimir"
    error_message = "datasource_uids.prometheus must be used for the rules."
  }
}

run "missing_directories_provision_nothing" {
  command = plan

  module {
    source = "../../modules/grafana"
  }

  variables {
    dashboards_dir = "./tests/fixtures/grafana/does-not-exist"
    alerting_dir   = "./tests/fixtures/grafana/does-not-exist"
  }

  assert {
    condition     = length(grafana_dashboard.this) == 0 && length(grafana_rule_group.this) == 0 && length(grafana_folder.this) == 0
    error_message = "Before deploy/grafana exists, the module must plan cleanly with nothing in it."
  }
}
