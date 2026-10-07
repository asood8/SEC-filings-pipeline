import logging
import subprocess
import sys
from collections.abc import Sequence
from typing import Any, ClassVar

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http import trace_exporter as otlp
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from filings_common.config import TelemetrySettings
from filings_common.telemetry import (
    LOG_FORMAT,
    TraceContextFormatter,
    build_tracer_provider,
    context_from_headers,
    continue_from,
    current_trace_context,
    filing_trace,
    kafka_headers,
    parse_pairs,
    record_error,
    redacted_span,
    setup_telemetry,
)

tracer = trace.get_tracer(__name__)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
UPSTREAM = {"traceparent": f"00-{TRACE_ID}-{SPAN_ID}-01"}
WEBHOOK = "https://discord.com/api/webhooks/1/secret-token"


class FakeExporter(SpanExporter):
    """Stands in for the OTLP exporter: records how it was built and never sends anything."""

    built: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        FakeExporter.built = kwargs

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS


def settings(**values: Any) -> TelemetrySettings:
    return TelemetrySettings(_env_file=None, **values)


def only(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    [span] = [s for s in spans.get_finished_spans() if s.name == name]
    return span


# --- Setup ---------------------------------------------------------------------------------------


def test_tracing_is_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(provider: object) -> None:
        raise AssertionError("no tracer provider may be installed when tracing is off")

    monkeypatch.setattr(trace, "set_tracer_provider", refuse)
    off = setup_telemetry("filings-test", settings=settings())
    assert not off.enabled
    off.shutdown()  # nothing to flush
    assert build_tracer_provider(settings(exporter_otlp_endpoint="  "), "filings-test") is None


def test_tracing_off_does_not_even_load_the_exporter() -> None:
    """Checked in a fresh interpreter: this one has already imported it."""
    code = (
        "import sys\n"
        "from filings_common.telemetry import setup_telemetry\n"
        "assert not setup_telemetry('filings-test').enabled\n"
        "print(sorted(m for m in sys.modules if m.startswith('opentelemetry.exporter')))\n"
    )
    # The endpoint is blank in this process's environment (conftest.py), and so in the child's.
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert child.stdout.strip() == "[]"


def test_sdk_disabled_turns_tracing_off_despite_an_endpoint() -> None:
    disabled = settings(exporter_otlp_endpoint="http://collector:4318", sdk_disabled=True)
    assert build_tracer_provider(disabled, "filings-test") is None


def test_provider_exports_over_otlp_http_with_the_configured_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(otlp, "OTLPSpanExporter", FakeExporter)
    configured = settings(
        exporter_otlp_endpoint="https://otlp-gateway.example.net/otlp/",
        exporter_otlp_headers="Authorization=Basic%20c2VjcmV0, X-Scope-OrgID=t1",
        service_name="parser-canary",
        resource_attributes="deployment.environment.name=production,service.namespace=filings",
    )
    provider = build_tracer_provider(configured, "filings-parser", "1.2.3")
    assert provider is not None
    try:
        assert FakeExporter.built["endpoint"] == "https://otlp-gateway.example.net/otlp/v1/traces"
        assert FakeExporter.built["headers"] == {
            "Authorization": "Basic c2VjcmV0",
            "X-Scope-OrgID": "t1",
        }
        attributes = provider.resource.attributes
        assert attributes["service.name"] == "parser-canary"  # OTEL_SERVICE_NAME wins
        assert attributes["service.version"] == "1.2.3"
        assert attributes["deployment.environment.name"] == "production"
        assert attributes["service.namespace"] == "filings"
    finally:
        provider.shutdown()


def test_setup_installs_the_provider_and_never_logs_the_headers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(otlp, "OTLPSpanExporter", FakeExporter)
    installed: list[object] = []
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    configured = settings(
        exporter_otlp_endpoint="https://otlp-gateway.example.net/otlp",
        exporter_otlp_headers="Authorization=Basic%20c2VjcmV0",
    )
    with caplog.at_level(logging.INFO):
        on = setup_telemetry("filings-common", settings=configured)
    try:
        assert on.enabled
        assert installed == [on.provider]
        assert "otlp-gateway.example.net" in caplog.text
        assert "c2VjcmV0" not in caplog.text
        assert "c2VjcmV0" not in repr(configured)  # SecretStr keeps it out of reprs
    finally:
        on.shutdown()


def test_parse_pairs_url_decodes_and_never_quotes_the_text() -> None:
    assert parse_pairs("a=1,, b = x%20y ", "S") == {"a": "1", "b": "x y"}
    with pytest.raises(ValueError, match="item 2 is not key=value") as error:
        parse_pairs("Authorization=Basic%20abc,hunter2", "OTEL_EXPORTER_OTLP_HEADERS")
    assert "hunter2" not in str(error.value)
    assert "abc" not in str(error.value)


# --- Context propagation -------------------------------------------------------------------------


def test_no_trace_context_without_an_active_span() -> None:
    assert current_trace_context() is None


def test_context_survives_the_outbox_and_kafka_headers(spans: InMemorySpanExporter) -> None:
    with tracer.start_as_current_span("write") as write:
        stored = current_trace_context()
    assert stored is not None
    assert trace.format_trace_id(write.get_span_context().trace_id) in stored["traceparent"]

    publish = tracer.start_span("send", context=continue_from(stored))
    headers = kafka_headers(publish)
    publish.end()
    assert [key for key, _ in headers] == ["traceparent"]
    with tracer.start_as_current_span("process", context=context_from_headers(headers)):
        pass

    send, process = only(spans, "send"), only(spans, "process")
    assert send.parent is not None
    assert send.parent.span_id == write.get_span_context().span_id
    assert process.parent is not None
    assert process.parent.span_id == send.context.span_id
    assert len({s.context.trace_id for s in spans.get_finished_spans()}) == 1


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("traceparent", b"not a traceparent")],
        [("traceparent", b"\xff\xfe")],
        [("dlq.source", f"00-{TRACE_ID}-{SPAN_ID}-01".encode())],  # not a trace header
    ],
)
def test_missing_or_invalid_headers_start_a_new_trace(
    spans: InMemorySpanExporter, headers: list[tuple[str, bytes]]
) -> None:
    with tracer.start_as_current_span("process", context=context_from_headers(headers)):
        pass
    assert only(spans, "process").parent is None


def test_with_tracing_off_an_upstream_context_passes_through_unchanged() -> None:
    """A process with tracing off still forwards the trace it received, so traces don't break
    at it. The API's no-op tracer (what every tracer is without an SDK provider) does this."""
    off = trace.NoOpTracerProvider().get_tracer(__name__)
    publish = off.start_span("send", context=continue_from(UPSTREAM))
    assert kafka_headers(publish) == (("traceparent", UPSTREAM["traceparent"].encode()),)
    with off.start_as_current_span("process", context=context_from_headers(kafka_headers(publish))):
        assert current_trace_context() == UPSTREAM

    fresh = off.start_span("send", context=continue_from(None))  # no upstream: nothing to send
    assert kafka_headers(fresh) == ()


def test_filing_trace_starts_a_new_trace_linked_to_the_current_span(
    spans: InMemorySpanExporter,
) -> None:
    with (
        tracer.start_as_current_span("poll") as poll,
        filing_trace(tracer, "discover filing", "0000320193-26-000101", form_type="8-K", cik=1),
    ):
        pass
    with filing_trace(tracer, "replay", "0000320193-26-000102", form_type="4", cik=1):
        pass  # no current span: nothing to link to

    discovered, replayed = only(spans, "discover filing"), only(spans, "replay")
    assert discovered.parent is None
    assert discovered.context.trace_id != poll.get_span_context().trace_id
    assert [link.context for link in discovered.links] == [poll.get_span_context()]
    assert discovered.attributes == {
        "filings.accession_no": "0000320193-26-000101",
        "filings.form_type": "8-K",
        "filings.cik": 1,
    }
    assert replayed.parent is None
    assert not replayed.links


# --- Errors and privacy --------------------------------------------------------------------------


def test_redacted_span_records_only_the_exception_type(spans: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError, match="secret-token"), redacted_span(tracer, "deliver"):
        raise ValueError(f"rejected {WEBHOOK}")

    span = only(spans, "deliver")
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError"
    assert [(e.name, dict(e.attributes or {})) for e in span.events] == [
        ("exception", {"exception.type": "ValueError"})
    ]
    assert "secret-token" not in span.to_json()


def test_record_error_keeps_the_message_unless_redacted(spans: InMemorySpanExporter) -> None:
    with tracer.start_as_current_span("plain") as plain:
        record_error(plain, ConnectionError("database restarting"))
    with tracer.start_as_current_span("private") as private:
        record_error(private, RuntimeError(WEBHOOK), redact=True)

    assert only(spans, "plain").status.description == "ConnectionError: database restarting"
    assert "database restarting" in only(spans, "plain").to_json()
    assert only(spans, "private").status.description == "RuntimeError"
    assert "secret-token" not in only(spans, "private").to_json()


# --- Log correlation -----------------------------------------------------------------------------


def test_log_lines_carry_the_active_trace(spans: InMemorySpanExporter) -> None:
    formatter = TraceContextFormatter(LOG_FORMAT)

    def line() -> str:
        record = logging.LogRecord("filings", logging.INFO, __file__, 1, "parsed %s", ("x",), None)
        return formatter.format(record)

    assert "trace_id=" not in line()
    with tracer.start_as_current_span("work") as span:
        context = span.get_span_context()
        assert line().endswith(
            f"parsed x trace_id={trace.format_trace_id(context.trace_id)}"
            f" span_id={trace.format_span_id(context.span_id)}"
        )
