"""The Grafana dashboards and alert rules in deploy/grafana stay in step with the code (ADR-0016).

They are provisioned as they are, into the local viewer and (by Terraform) into Grafana Cloud, so
a renamed metric or label would only show up as an empty panel, or an alert that never fires.
These tests check every query against the metrics the services define, and the files' structure
against what Grafana expects.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from opentelemetry.metrics import Counter, Histogram

from filings_common import metrics, telemetry

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

ROOT = Path(__file__).resolve().parents[1]
GRAFANA = ROOT / "deploy" / "grafana"
DASHBOARD_FILES = sorted((GRAFANA / "dashboards").glob("*.json"))
ALERT_RULES = GRAFANA / "alerting" / "filings.yaml"
DASHBOARD_PROVIDER = GRAFANA / "local" / "dashboards.yaml"

# The FastAPI instrumentation's metrics (stable HTTP semantic conventions) and the resource
# attributes Prometheus and Grafana Cloud store for every process.
HTTP_SERVER_METRICS = {
    "http_server_request_duration_seconds_bucket",
    "http_server_request_duration_seconds_sum",
    "http_server_request_duration_seconds_count",
    "http_server_active_requests",
    "target_info",
}
# Labels besides the metrics' attributes: histogram buckets, and service.name and
# service.instance.id as Prometheus translates them.
PROMETHEUS_LABELS = {"le", "job", "instance"}
ATTRIBUTES = {
    telemetry.FORM_TYPE,
    telemetry.OUTCOME,
    telemetry.MESSAGING_DESTINATION,
    telemetry.MESSAGING_PARTITION,
    telemetry.MESSAGING_CONSUMER_GROUP,
    telemetry.GEN_AI_PROVIDER,
    metrics.ALERT_CHANNEL,
    metrics.STATUS_CLASS,
    "http.request.method",
    "http.route",
    "http.response.status_code",
    "url.scheme",
}
KNOWN_LABELS = PROMETHEUS_LABELS | {a.replace(".", "_") for a in ATTRIBUTES}

# Metrics only short-lived jobs record (discovery): one series per run, mostly with a single value,
# which rate() and increase() can't use. Queries must add up runs with max_over_time.
JOB_METRIC = re.compile(r"\b(filings_discovered_total|filings_sec_\w+)")

# PromQL pieces.
STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
MATCHERS = re.compile(r"\{([^{}]*)\}")
GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^()]*)\)")
IDENTIFIER = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")
LABEL_REPLACE_TARGETS = re.compile(
    r'"([a-zA-Z_]\w*)"\s*,\s*"[^"]*"\s*,\s*"(\w*)"\s*,\s*"[^"]*"\s*\)'
)


def _prometheus_unit(unit: str) -> str:
    if unit == "s":
        return "_seconds"
    assert re.fullmatch(r"\{\w+\}", unit), f"unit {unit!r}: add its Prometheus suffix here"
    return ""  # annotations such as {filing} don't become part of the name


@pytest.fixture(scope="module")
def defined_metrics(_metric_reader: "InMemoryMetricReader") -> set[str]:
    """The Prometheus names of every metric in `filings_common.metrics`, from what the SDK
    actually exports for each instrument (name, unit, and kind)."""
    from opentelemetry.sdk.metrics.export import Gauge, Sum
    from opentelemetry.sdk.metrics.export import Histogram as HistogramData

    _metric_reader.get_metrics_data()  # drop what earlier tests recorded
    probe = {"probe": "grafana"}
    owner = object()
    for value in vars(metrics).values():
        if isinstance(value, Counter):
            value.add(0, probe)
        elif isinstance(value, Histogram):
            value.record(0, probe)
    gauges = (metrics.CONSUMER_LAG_VALUES, metrics.DLQ_DEPTH_VALUES)
    for gauge in gauges:
        gauge.replace(owner, [(0, probe)])
    try:
        data = _metric_reader.get_metrics_data()
    finally:
        for gauge in gauges:
            gauge.remove(owner)
    assert data is not None
    names = set()
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if not metric.name.startswith("filings."):
                    continue
                base = metric.name.replace(".", "_") + _prometheus_unit(metric.unit or "")
                if isinstance(metric.data, Sum) and metric.data.is_monotonic:
                    names.add(f"{base}_total")
                elif isinstance(metric.data, Sum | Gauge):
                    names.add(base)
                elif isinstance(metric.data, HistogramData):
                    names |= {f"{base}_bucket", f"{base}_sum", f"{base}_count"}
    assert "filings_consumer_lag" in names  # the observable gauges were read too
    return names


def _dashboards() -> Iterator[tuple[Path, dict[str, Any]]]:
    for path in DASHBOARD_FILES:
        yield path, json.loads(path.read_text())


def _alert_rules() -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(ALERT_RULES.read_text())
    return rules


def _panels(dashboard: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for panel in dashboard["panels"]:
        yield panel
        yield from panel.get("panels", [])  # inside collapsed rows


def _dashboard_queries() -> Iterator[tuple[str, str]]:
    """(where, PromQL) for every Prometheus query in the dashboards."""
    for path, dashboard in _dashboards():
        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                if target["datasource"]["type"] == "prometheus":
                    yield f"{path.name}: {panel['title']} ({target['refId']})", target["expr"]


def _prometheus_queries() -> Iterator[tuple[str, str]]:
    """(where, PromQL) for every Prometheus query in the dashboards and alert rules."""
    yield from _dashboard_queries()
    for group in _alert_rules()["groups"]:
        for rule in group["rules"]:
            for query in rule["data"]:
                if query["datasourceUid"] != "__expr__":
                    yield f"{ALERT_RULES.name}: {rule['title']}", query["model"]["expr"]


def check_promql(expr: str, defined: set[str]) -> list[str]:
    """Problems with the metric and label names in `expr` (a few regexes, not a PromQL parser:
    enough for the queries here)."""
    problems = []
    created = {target for target, _ in LABEL_REPLACE_TARGETS.findall(expr)}
    for _, source in LABEL_REPLACE_TARGETS.findall(expr):
        if source and source not in KNOWN_LABELS | created:
            problems.append(f"label_replace reads unknown label {source!r}")
    labels = KNOWN_LABELS | created
    code = STRING.sub('""', expr)
    for matchers in MATCHERS.findall(code):
        for matcher in filter(None, (m.strip() for m in matchers.split(","))):
            label = re.split(r"\s*(?:=~|!~|!=|=)\s*", matcher, maxsplit=1)[0]
            if label not in labels:
                problems.append(f"unknown label {label!r}")
    for grouping in GROUPING.findall(code):
        for label in filter(None, (g.strip() for g in grouping.split(","))):
            if label not in labels:
                problems.append(f"unknown label {label!r}")
    bare = GROUPING.sub(" ", MATCHERS.sub(" ", code))
    for name in IDENTIFIER.findall(bare):
        if name.startswith(("filings_", "http_server_", "target_")) and name not in (
            defined | HTTP_SERVER_METRICS
        ):
            problems.append(f"unknown metric {name!r}")
    for match in JOB_METRIC.finditer(code):
        if not re.search(r"max_over_time\(\s*$", code[: match.start()]):
            problems.append(f"{match[1]} comes from a job: add up runs with max_over_time")
    return problems


def test_queries_use_only_defined_metrics_and_labels(defined_metrics: set[str]) -> None:
    queries = list(_prometheus_queries())
    assert len(queries) > 40
    problems = [
        f"{where}: {p}" for where, expr in queries for p in check_promql(expr, defined_metrics)
    ]
    assert problems == []


def test_the_checks_catch_mistakes(defined_metrics: set[str]) -> None:
    assert check_promql("sum by (messaging_consumer_group) (filings_consumer_lag)", defined_metrics)
    assert check_promql('filings_consumer_lags{messaging_destination_name="x"}', defined_metrics)
    assert check_promql("sum(increase(filings_discovered_total[1h]))", defined_metrics)
    assert check_promql('label_replace(up, "x", "$1", "nope", "(.*)")', defined_metrics)
    assert not check_promql(
        "sum by (status_class) (label_replace(rate(http_server_request_duration_seconds_count"
        '[5m]), "status_class", "${1}xx", "http_response_status_code", "([0-9])[0-9][0-9]"))',
        defined_metrics,
    )


def test_every_metric_is_on_a_dashboard(defined_metrics: set[str]) -> None:
    used = " ".join(expr for _, expr in _dashboard_queries())
    # A histogram counts as shown when any of its _bucket, _sum, or _count series is.
    bases = {re.sub(r"_(bucket|sum|count)$", "", name) for name in defined_metrics}
    assert {base for base in bases if not re.search(rf"\b{base}", used)} == set()


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_dashboards_are_portable_and_well_formed(path: Path) -> None:
    dashboard = json.loads(path.read_text())
    # Grafana assigns numeric IDs itself; the UID is what links (and alert rules) point to.
    assert "id" not in dashboard
    assert re.fullmatch(r"[a-z0-9-]{1,40}", dashboard["uid"])
    assert "sec-filings" in dashboard["tags"]
    # Data sources are variables, so the same JSON works locally and on Grafana Cloud, whose
    # data sources have other UIDs.
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert variables["datasource"]["query"] == "prometheus"
    ids: set[int] = set()
    cells: set[tuple[int, int]] = set()  # grid cells (column, row) taken by earlier panels
    for panel in _panels(dashboard):
        assert panel["id"] not in ids, f"duplicate panel id {panel['id']}"
        ids.add(panel["id"])
        x, y, w, h = (panel["gridPos"][k] for k in ("x", "y", "w", "h"))
        assert x >= 0, panel["title"]
        assert x + w <= 24, panel["title"]
        covered = {(col, row) for col in range(x, x + w) for row in range(y, y + h)}
        assert not covered & cells, f"{panel['title']} overlaps another panel"
        cells |= covered
        if panel["type"] == "row":
            continue
        assert panel.get("description"), f"{panel['title']} needs a description"
        for target in [panel, *panel["targets"]]:
            kind = target["datasource"]["type"]
            variable = {"prometheus": "datasource", "tempo": "tempo"}[kind]
            assert target["datasource"]["uid"] == f"${{{variable}}}"
            assert variables[variable]["query"] == kind


def test_dashboard_uids_and_titles_are_unique() -> None:
    boards = [board for _, board in _dashboards()]
    assert len(boards) >= 2
    assert len({b["uid"] for b in boards}) == len(boards)
    assert len({b["title"] for b in boards}) == len(boards)


def test_alert_rules_are_well_formed() -> None:
    provider = yaml.safe_load(DASHBOARD_PROVIDER.read_text())["providers"][0]
    panels = {board["uid"]: {p["id"] for p in _panels(board)} for _, board in _dashboards()}
    rules = [rule for group in _alert_rules()["groups"] for rule in group["rules"]]
    for group in _alert_rules()["groups"]:
        assert group["folder"] == provider["folder"]  # next to the dashboards
    assert len({r["uid"] for r in rules}) == len(rules)
    for rule in rules:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,40}", rule["uid"])
        queries = {q["refId"]: q for q in rule["data"]}
        assert rule["condition"] in queries
        for ref, query in queries.items():
            assert query["model"]["refId"] == ref
            if query["datasourceUid"] == "__expr__":
                assert query["model"]["expression"] in queries
            else:
                # The one value Terraform replaces for Grafana Cloud.
                assert query["datasourceUid"] == "prometheus"
                assert query["model"]["instant"] is True
        assert rule["noDataState"] in {"OK", "NoData", "Alerting", "KeepLast"}
        assert rule["execErrState"] in {"OK", "Error", "Alerting", "KeepLast"}
        assert re.fullmatch(r"\d+[smh]", rule["for"])
        assert rule["dashboardUid"] in panels
        if "panelId" in rule:
            assert rule["panelId"] in panels[rule["dashboardUid"]]
        # Grafana would read "$labels" in a provisioning file as an environment variable.
        for text in [*rule["annotations"].values(), *rule["labels"].values()]:
            assert "$" not in text, rule["title"]
        assert rule["labels"]["severity"] in {"warning", "critical"}


def test_the_briefs_alerts_are_all_there() -> None:
    uids = {rule["uid"] for group in _alert_rules()["groups"] for rule in group["rules"]}
    assert {"filings-no-new-filings", "filings-dlq-not-empty", "filings-consumer-lag-high"} <= uids


def test_the_local_viewer_mounts_files_that_exist() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["otel-lgtm"]
    assert service["profiles"] == ["observability"]
    assert re.fullmatch(r"grafana/otel-lgtm:\d+\.\d+\.\d+", service["image"])  # pinned
    assert all(port.startswith("127.0.0.1:") for port in service["ports"])
    assert service["deploy"]["resources"]["limits"]["memory"]
    mounts = [v.split(":")[0] for v in service["volumes"] if v.startswith("./")]
    assert len(mounts) == 3
    for source in mounts:
        # A missing source would make Docker create an empty, root-owned directory instead.
        assert (ROOT / source).exists(), source
