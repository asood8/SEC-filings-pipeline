"""OpenTelemetry: setup for traces, metrics, and logs, trace context propagation, and log
correlation.

One trace follows a filing from discovery to alert. Its context (W3C `traceparent` and
`tracestate`) travels with the filing's events:
- `enqueue` stores the active span's context in the outbox row, in the same transaction;
- the relay publishes the row under a span that continues that context, and sends its own context
  in the Kafka message headers;
- the consume loop runs the handler in a span that continues the headers' context, so the handler's
  own outbox writes carry the trace on to the next stage.

Every process calls `setup_telemetry` once at startup. Telemetry is off unless
OTEL_EXPORTER_OTLP_ENDPOINT is set: the OpenTelemetry API's no-op tracer and meter then stay in
place, so spans and measurements cost next to nothing and nothing is exported, but a context
received from upstream still passes through unchanged. The SDK and the exporters are only imported
when telemetry is on. The pipeline's metrics are defined in `filings_common.metrics`.

Log lines always go to stderr; with telemetry on they are also exported, with their trace, so they
outlast the pod (`export_logs`).

Privacy: spans and log lines never carry webhook URLs, email addresses, tokens, API keys, or LLM
prompt and response text. Where an exception's message might quote one, a span records only its
type (`record_error(..., redact=True)`, `redacted_span`).
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes, AttributeValue

from filings_common.config import TelemetrySettings

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.view import View
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Log lines exported per process and minute, at most. The services log a few lines per filing,
# poll, or alert, far below this, but a fault that logs in a loop must not use up Grafana Cloud's
# free 50 GB of logs a month (ADR-0016): seven services at this limit all month would send about
# 11 GB. Lines over the limit still reach stderr.
LOG_EXPORT_LIMIT_PER_MINUTE = 120

# Span attributes for the pipeline's own concepts. An accession number is fine on a span, but must
# never become a metric label: it is unique per filing.
ACCESSION_NO = "filings.accession_no"
FORM_TYPE = "filings.form_type"
CIK = "filings.cik"
OUTCOME = "filings.outcome"
RATE_LIMIT_WAIT = "filings.rate_limit.wait_seconds"

# OpenTelemetry semantic conventions (https://opentelemetry.io/docs/specs/semconv/).
MESSAGING_SYSTEM = "messaging.system"
MESSAGING_OPERATION = "messaging.operation.type"
MESSAGING_DESTINATION = "messaging.destination.name"
MESSAGING_PARTITION = "messaging.destination.partition.id"
MESSAGING_OFFSET = "messaging.kafka.offset"
MESSAGING_CONSUMER_GROUP = "messaging.consumer.group.name"
MESSAGING_KEY = "messaging.kafka.message.key"
MESSAGING_BATCH_SIZE = "messaging.batch.message_count"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_PROVIDER = "gen_ai.provider.name"
GEN_AI_MODEL = "gen_ai.request.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# W3C Trace Context only, whatever OTEL_PROPAGATORS says: it is what the outbox and Kafka carry.
_PROPAGATOR = TraceContextTextMapPropagator()

KafkaHeaders = tuple[tuple[str, bytes], ...]


# Instruments whose data isn't worth its series (the free tier allows 10,000; ADR-0016): request
# and response sizes per API route, and the old HTTP duration metric, which has no route but does
# have the client-controlled Host header as an attribute.
DROPPED_INSTRUMENTS = (
    "http.server.request.body.size",
    "http.server.response.body.size",
    "http.server.request.size",
    "http.server.response.size",
    "http.server.duration",
)


@dataclass(frozen=True)
class Telemetry:
    """What `setup_telemetry` installed. Call `shutdown` once, on the way out."""

    tracer_provider: "TracerProvider | None" = None
    meter_provider: "MeterProvider | None" = None
    logger_provider: "LoggerProvider | None" = None
    # The handler `export_logs` added, and the loggers it was added to.
    log_handler: logging.Handler | None = None
    log_sources: tuple[logging.Logger, ...] = field(default=())

    @property
    def enabled(self) -> bool:
        return any(
            provider is not None
            for provider in (self.tracer_provider, self.meter_provider, self.logger_provider)
        )

    def shutdown(self) -> None:
        """Export the spans, metrics, and log lines still buffered, then stop exporting."""
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.log_handler is not None:
            for source in self.log_sources:
                source.removeHandler(self.log_handler)
        if self.logger_provider is not None:
            self.logger_provider.shutdown()


def setup_telemetry(
    service_name: str,
    *,
    distribution: str | None = None,
    settings: TelemetrySettings | None = None,
) -> Telemetry:
    """Configure logging and, if an endpoint is set, traces, metrics, and logs for this process.

    `service.version` is the installed version of `distribution` (by default, the package named
    like the service).
    """
    configure_logging()
    settings = settings or TelemetrySettings()
    if not any(
        _exports(settings, exporter)
        for exporter in (
            settings.traces_exporter,
            settings.metrics_exporter,
            settings.logs_exporter,
        )
    ):
        logger.info("telemetry is off (OTEL_EXPORTER_OTLP_ENDPOINT is not set)")
        return Telemetry()
    resource = build_resource(
        settings, service_name, _installed_version(distribution or service_name)
    )
    tracer_provider = build_tracer_provider(settings, resource)
    meter_provider = build_meter_provider(settings, resource)
    logger_provider = build_logger_provider(settings, resource)
    if tracer_provider is not None:
        trace.set_tracer_provider(tracer_provider)
    if meter_provider is not None:
        metrics.set_meter_provider(meter_provider)
    log_handler, log_sources = (
        export_logs(logger_provider) if logger_provider is not None else (None, ())
    )
    signals = [
        name
        for name, provider in (
            ("traces", tracer_provider),
            ("metrics", meter_provider),
            ("logs", logger_provider),
        )
        if provider is not None
    ]
    listed = ", ".join(signals[:-1]) + " and " + signals[-1] if len(signals) > 1 else signals[0]
    host = urlsplit(settings.exporter_otlp_endpoint or "").hostname
    logger.info("exporting %s to %s over OTLP/HTTP", listed, host)
    return Telemetry(tracer_provider, meter_provider, logger_provider, log_handler, log_sources)


def _exports(settings: TelemetrySettings, exporter: str) -> bool:
    """Whether a signal whose OTEL_<SIGNAL>_EXPORTER is `exporter` is exported."""
    return (
        not settings.sdk_disabled
        and settings.exporter_otlp_endpoint is not None
        and exporter == "otlp"
    )


def build_resource(
    settings: TelemetrySettings, service_name: str, service_version: str | None = None
) -> "Resource":
    """What every span and metric of this process is about: the service, its version, and this
    instance of it.

    `service.instance.id` is a random ID per process unless OTEL_RESOURCE_ATTRIBUTES sets one (in
    Kubernetes, the pod's name). Each process must have its own: metrics are cumulative counts per
    process, and two replicas, or two runs of a job, writing into one series would mix them up.
    """
    # Imported here, so that with telemetry off a process doesn't load the SDK, the exporters,
    # and protobuf (about 5 MB of memory).
    from opentelemetry.sdk.resources import (
        SERVICE_INSTANCE_ID,
        SERVICE_NAME,
        SERVICE_VERSION,
        Resource,
    )

    attributes: dict[str, AttributeValue] = {SERVICE_INSTANCE_ID: str(uuid.uuid4())}
    attributes |= parse_pairs(settings.resource_attributes or "", "OTEL_RESOURCE_ATTRIBUTES")
    attributes[SERVICE_NAME] = settings.service_name or service_name
    if service_version is not None:
        attributes[SERVICE_VERSION] = service_version
    return Resource.create(attributes)


def build_tracer_provider(
    settings: TelemetrySettings, resource: "Resource"
) -> "TracerProvider | None":
    """A tracer provider that batches spans and exports them over OTLP/HTTP, or None when traces
    are off. Sampling follows OTEL_TRACES_SAMPLER (default: every trace)."""
    if not _exports(settings, settings.traces_exporter):
        return None
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    exporter = OTLPSpanExporter(
        endpoint=_signal_endpoint(settings, "traces"),
        headers=_headers(settings),
        compression=Compression.Gzip,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def build_meter_provider(
    settings: TelemetrySettings, resource: "Resource"
) -> "MeterProvider | None":
    """A meter provider that exports every metric's current value over OTLP/HTTP once per
    OTEL_METRIC_EXPORT_INTERVAL, or None when metrics are off.

    Values are cumulative (totals since the process started), which is what Prometheus and
    Grafana Cloud store; their queries turn totals into rates.
    """
    if not _exports(settings, settings.metrics_exporter):
        return None
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    exporter = OTLPMetricExporter(
        endpoint=_signal_endpoint(settings, "metrics"),
        headers=_headers(settings),
        compression=Compression.Gzip,
    )
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=settings.metric_export_interval
    )
    return MeterProvider(metric_readers=[reader], resource=resource, views=metric_views())


def build_logger_provider(
    settings: TelemetrySettings, resource: "Resource"
) -> "LoggerProvider | None":
    """A logger provider that batches log records and exports them over OTLP/HTTP, or None when
    logs are off (OTEL_LOGS_EXPORTER=none)."""
    if not _exports(settings, settings.logs_exporter):
        return None
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    exporter = OTLPLogExporter(
        endpoint=_signal_endpoint(settings, "logs"),
        headers=_headers(settings),
        compression=Compression.Gzip,
    )
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    return provider


def export_logs(provider: "LoggerProvider") -> tuple[logging.Handler, tuple[logging.Logger, ...]]:
    """Also send this process's log lines (INFO and up) to `provider`, each with the trace that
    was active. Returns the handler and the loggers it was added to, for `Telemetry.shutdown`.

    It goes on the root logger and, under uvicorn, on uvicorn's own logger, which doesn't
    propagate (server start and stop, and errors in the app). uvicorn's access log, one line per
    request, stays on stderr only: each request already has a span.
    """
    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    handler.addFilter(LogExportFilter())
    sources = [logging.getLogger()]
    uvicorn = logging.getLogger("uvicorn")
    if not uvicorn.propagate:  # uvicorn configured its loggers
        sources.append(uvicorn)
    for source in sources:
        source.addHandler(handler)
    return handler, tuple(sources)


class LogExportFilter(logging.Filter):
    """Which log records are exported: at most `per_minute` a minute (one stderr line says when
    the rest of a minute's aren't), and none of the OpenTelemetry SDK's own, whose export
    failures would otherwise be exported in turn. Every record still reaches stderr."""

    def __init__(
        self,
        per_minute: int = LOG_EXPORT_LIMIT_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._per_minute = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._minute = -1
        self._count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "opentelemetry" or record.name.startswith("opentelemetry."):
            return False
        with self._lock:
            minute = int(self._clock() // 60)
            if minute != self._minute:
                self._minute, self._count = minute, 0
            self._count += 1
            count = self._count
        if count == self._per_minute + 1:  # logged outside the lock: it comes back here
            logger.warning(
                "over %d log lines this minute: the rest of this minute's are not exported",
                self._per_minute,
            )
        return count <= self._per_minute


def metric_views() -> "list[View]":
    """Views that drop the instruments in DROPPED_INSTRUMENTS."""
    from opentelemetry.sdk.metrics.view import DropAggregation, View

    return [
        View(instrument_name=name, aggregation=DropAggregation()) for name in DROPPED_INSTRUMENTS
    ]


def _signal_endpoint(settings: TelemetrySettings, signal: str) -> str:
    return f"{(settings.exporter_otlp_endpoint or '').rstrip('/')}/v1/{signal}"


def _headers(settings: TelemetrySettings) -> dict[str, str] | None:
    headers = settings.exporter_otlp_headers
    if headers is None:
        return None
    return parse_pairs(headers.get_secret_value(), "OTEL_EXPORTER_OTLP_HEADERS")


def parse_pairs(text: str, setting: str) -> dict[str, str]:
    """`key=value,key=value` with URL-encoded values, the format of OTEL_EXPORTER_OTLP_HEADERS and
    OTEL_RESOURCE_ATTRIBUTES. Errors never quote the text: headers hold credentials."""
    pairs = {}
    for position, item in enumerate(text.split(","), start=1):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"{setting}: item {position} is not key=value")
        pairs[unquote(key).strip()] = unquote(value).strip()
    return pairs


def _installed_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


class TraceContextFormatter(logging.Formatter):
    """Adds the active span's trace and span IDs to each log line, to find a trace's logs."""

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 (overrides logging)
        message = super().formatMessage(record)
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return message
        trace_id = trace.format_trace_id(context.trace_id)
        span_id = trace.format_span_id(context.span_id)
        return f"{message} trace_id={trace_id} span_id={span_id}"


def configure_logging(level: int = logging.INFO) -> None:
    """Log to stderr in the services' common format, with trace IDs when a span is active. Does
    nothing if the root logger already has handlers (uvicorn configures only its own loggers)."""
    handler = logging.StreamHandler()
    handler.setFormatter(TraceContextFormatter(LOG_FORMAT))
    logging.basicConfig(level=level, handlers=[handler])


def current_trace_context() -> dict[str, str] | None:
    """The active span's trace context, to store with an outbox event. None without a span."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier or None


def continue_from(trace_context: Mapping[str, str] | None) -> Context:
    """A context whose current span is the one `trace_context` came from, to start a child of it.
    Without a valid trace context it is empty, so the next span starts a new trace."""
    return _PROPAGATOR.extract(trace_context or {}, context=Context())


def kafka_headers(span: Span) -> KafkaHeaders:
    """Kafka message headers carrying `span`'s trace context."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier, context=trace.set_span_in_context(span))
    return tuple((key, value.encode()) for key, value in carrier.items())


def context_from_headers(headers: Iterable[tuple[str, bytes]]) -> Context:
    """`continue_from` the trace context in Kafka message headers."""
    carrier = {}
    for key, value in headers:
        if key in _PROPAGATOR.fields:
            try:
                carrier[key] = value.decode()
            except UnicodeDecodeError:
                continue
    return continue_from(carrier)


def tag_filing(accession_no: str, *, form_type: str, cik: int) -> None:
    """Name the filing the current span works on (to find its trace by accession number)."""
    trace.get_current_span().set_attributes(
        {ACCESSION_NO: accession_no, FORM_TYPE: form_type, CIK: cik}
    )


@contextmanager
def filing_trace(
    tracer: Tracer, name: str, accession_no: str, *, form_type: str, cik: int
) -> Iterator[Span]:
    """Start a filing's own trace: a new root span, linked to the current span (such as the poll
    that found the filing). Outbox events written inside carry this trace to the next stages, so
    each filing gets one trace from here to its alert instead of sharing the poll's."""
    current = trace.get_current_span().get_span_context()
    with tracer.start_as_current_span(
        name,
        context=Context(),  # no parent: a new trace
        links=[trace.Link(current)] if current.is_valid else None,
        attributes={ACCESSION_NO: accession_no, FORM_TYPE: form_type, CIK: cik},
    ) as span:
        yield span


def exception_type(exc: BaseException) -> str:
    """The exception's fully qualified type name, as OpenTelemetry records it."""
    module, name = type(exc).__module__, type(exc).__qualname__
    return name if module == "builtins" else f"{module}.{name}"


def record_exception(
    span: Span, exc: BaseException, *, redact: bool = False, attributes: Attributes = None
) -> None:
    """Add an exception event to `span`. With `redact`, it holds only the exception's type: its
    message and stack trace could quote a webhook URL, an email address, or LLM text."""
    if redact:
        span.add_event("exception", {"exception.type": exception_type(exc), **(attributes or {})})
    else:
        span.record_exception(exc, attributes)


def record_error(span: Span, exc: BaseException, *, redact: bool = False) -> None:
    """Record `exc` on `span` and mark the span failed."""
    record_exception(span, exc, redact=redact)
    kind = exception_type(exc)
    span.set_status(StatusCode.ERROR, kind if redact else f"{kind}: {exc}")


@contextmanager
def redacted_span(
    tracer: Tracer,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Attributes = None,
    context: Context | None = None,
) -> Iterator[Span]:
    """`tracer.start_as_current_span`, except that an exception escaping the block is recorded by
    its type only. For work whose errors may quote private data."""
    with tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception as exc:
            record_error(span, exc, redact=True)
            raise
