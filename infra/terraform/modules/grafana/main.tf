# Grafana Cloud: the repository's dashboards and alert rules, provisioned into an existing stack.
# The files in deploy/grafana are the source of truth, shared with the local viewer (ADR-0016);
# this module only reads them, so a dashboard is changed in one place.
#
# Left to a person: creating the Grafana Cloud account and stack, and a service account token
# for Terraform (infra/terraform/README.md, "Accounts and credentials"). Contact points and the
# notification policy also stay in the stack's UI: firing alerts go to its default contact point
# (email to the account owner) unless a rule names another, and contact points usually contain
# webhook secrets.
#
# Both directories are read with fileset(), which returns nothing for a directory that doesn't
# exist yet, so this module plans cleanly before any dashboard or rule is written.

locals {
  # --- Dashboards ---
  # The dashboards pick their data sources through template variables (the stack's default
  # Prometheus and Tempo), so they need no change. A panel that names the local viewer's data
  # source directly ("uid": "prometheus", as Grafana writes when a panel is exported) is
  # rewritten to the stack's.
  dashboards = {
    for file_name in fileset(var.dashboards_dir, "*.json") :
    trimsuffix(file_name, ".json") => replace(replace(replace(
      file("${var.dashboards_dir}/${file_name}"),
      "/(\"uid\"\\s*:\\s*)\"prometheus\"/", "$1\"${var.datasource_uids.prometheus}\""),
      "/(\"uid\"\\s*:\\s*)\"loki\"/", "$1\"${var.datasource_uids.loki}\""),
      "/(\"uid\"\\s*:\\s*)\"tempo\"/", "$1\"${var.datasource_uids.tempo}\"",
    )
  }

  # --- Alert rules ---
  # Every rule group in every file, keyed by "<folder>/<group name>" (unique in Grafana).
  rule_groups = merge([
    for file_name in fileset(var.alerting_dir, "*.{yaml,yml}") : {
      for group in try(yamldecode(file("${var.alerting_dir}/${file_name}")).groups, []) :
      "${group.folder}/${group.name}" => group
    }
  ]...)

  datasource_uid_map = {
    prometheus = var.datasource_uids.prometheus
    loki       = var.datasource_uids.loki
    tempo      = var.datasource_uids.tempo
  }

  # Group evaluation intervals such as "1m" or "30s", in seconds (the resource wants a number).
  duration_seconds = { s = 1, m = 60, h = 3600, d = 86400 }
  rule_group_intervals = {
    for key, group in local.rule_groups : key => (
      can(tonumber(try(group.interval, "x")))
      ? tonumber(group.interval)
      : tonumber(regex("^([0-9]+)[smhd]$", try(group.interval, "1m"))[0]) * local.duration_seconds[regex("^[0-9]+([smhd])$", try(group.interval, "1m"))[0]]
    )
  }

  # One folder per name used by the dashboards or a rule group.
  folders = toset(concat(
    length(local.dashboards) > 0 ? [var.dashboard_folder] : [],
    [for group in values(local.rule_groups) : group.folder],
  ))
}

resource "grafana_folder" "this" {
  for_each = local.folders

  title = each.value
  # "SEC Filings" -> "sec-filings" (folder UIDs allow at most 40 characters).
  uid = substr(trim(replace(lower(each.value), "/[^a-z0-9]+/", "-"), "-"), 0, 40)
}

resource "grafana_dashboard" "this" {
  for_each = local.dashboards

  folder      = grafana_folder.this[var.dashboard_folder].uid
  config_json = each.value
  # The files win over edits made in the UI.
  overwrite = true
}

resource "grafana_rule_group" "this" {
  for_each = local.rule_groups

  name             = each.value.name
  folder_uid       = grafana_folder.this[each.value.folder].uid
  interval_seconds = local.rule_group_intervals[each.key]

  # Fields map one to one from Grafana's alert provisioning file format
  # (https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/file-provisioning/).
  dynamic "rule" {
    for_each = each.value.rules

    content {
      name            = rule.value.title
      uid             = try(rule.value.uid, null)
      condition       = rule.value.condition
      for             = try(rule.value.for, "0s")
      no_data_state   = try(rule.value.noDataState, "NoData")
      exec_err_state  = try(rule.value.execErrState, "Error")
      keep_firing_for = try(rule.value.keepFiringFor, null)
      labels          = try(rule.value.labels, {})
      is_paused       = try(rule.value.isPaused, false)

      # The file's dashboardUid and panelId link a rule to a panel. The API takes them as two
      # special annotations and refuses one without the other, so a rule that names only a
      # dashboard gets no link.
      annotations = merge(
        try(rule.value.annotations, {}),
        can(rule.value.dashboardUid) && can(rule.value.panelId) ? {
          __dashboardUid__ = rule.value.dashboardUid
          __panelId__      = tostring(rule.value.panelId)
        } : {},
      )

      dynamic "data" {
        for_each = rule.value.data

        content {
          ref_id         = data.value.refId
          query_type     = try(data.value.queryType, "")
          datasource_uid = lookup(local.datasource_uid_map, data.value.datasourceUid, data.value.datasourceUid)
          model          = jsonencode(data.value.model)

          relative_time_range {
            from = try(data.value.relativeTimeRange.from, 0)
            to   = try(data.value.relativeTimeRange.to, 0)
          }
        }
      }

      dynamic "notification_settings" {
        for_each = try([rule.value.notification_settings], [])

        content {
          contact_point   = notification_settings.value.receiver
          group_by        = try(notification_settings.value.group_by, null)
          group_wait      = try(notification_settings.value.group_wait, null)
          group_interval  = try(notification_settings.value.group_interval, null)
          repeat_interval = try(notification_settings.value.repeat_interval, null)
          mute_timings    = try(notification_settings.value.mute_time_intervals, null)
        }
      }
    }
  }
}
