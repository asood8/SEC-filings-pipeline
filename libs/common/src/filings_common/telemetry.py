"""OpenTelemetry tracing: setup, trace context propagation, and log correlation.

One trace follows a filing from discovery to alert. Its context (W3C `traceparent` and
`tracestate`) travels with the filing's events:
- `enqueue` stores the active span's context in the outbox row, in the same transaction;
- the relay publishes the row under a span that continues that context, and sends its own context
  in the Kafka message headers;
- the consume loop runs the handler in a span that continues the headers' context, so the handler's
  own outbox writes carry the trace on to the next stage.

Every process calls `setup_telemetry` once at startup. Tracing is off unless
OTEL_EXPORTER_OTLP_ENDPOINT is set: the OpenTelemetry API's no-op tracer then stays in place, so
spans cost next to nothing and nothing is exported, but a context received from upstream still
passes through unchanged. The SDK and the exporter are only imported when tracing is on.

Privacy: spans never carry webhook URLs, email addresses, tokens, API keys, or LLM prompt and
response text. Where an exception's message might quote one, only its type is recorded
(`record_error(..., redact=True)`, `redacted_span`).
"""

import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes, AttributeValue

from filings_common.config import TelemetrySettings

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

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


@dataclass(frozen=True)
class Telemetry:
    """What `setup_telemetry` installed. Call `shutdown` once, on the way out."""

    provider: "TracerProvider | None" = None

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    def shutdown(self) -> None:
        """Export the spans still buffered, then stop exporting."""
        if self.provider is not None:
            self.provider.shutdown()


def setup_telemetry(
    service_name: str,
    *,
    distribution: str | None = None,
    settings: TelemetrySettings | None = None,
) -> Telemetry:
    """Configure logging and, if an endpoint is set, tracing for this process.

    `service.version` is the installed version of `distribution` (by default, the package named
    like the service).
    """
    configure_logging()
    settings = settings or TelemetrySettings()
    provider = build_tracer_provider(
        settings, service_name, _installed_version(distribution or service_name)
    )
    if provider is None:
        logger.info("tracing is off (OTEL_EXPORTER_OTLP_ENDPOINT is not set)")
        return Telemetry()
    trace.set_tracer_provider(provider)
    host = urlsplit(settings.exporter_otlp_endpoint or "").hostname
    logger.info("tracing on: exporting spans to %s over OTLP/HTTP", host)
    return Telemetry(provider)


def build_tracer_provider(
    settings: TelemetrySettings, service_name: str, service_version: str | None = None
) -> "TracerProvider | None":
    """A tracer provider that batches spans and exports them over OTLP/HTTP, or None when tracing
    is off. Sampling follows OTEL_TRACES_SAMPLER (default: every trace)."""
    if settings.sdk_disabled or settings.exporter_otlp_endpoint is None:
        return None
    # Imported here, so that with tracing off a process doesn't load the trace SDK, the exporter,
    # and protobuf (about 5 MB of memory).
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    attributes: dict[str, AttributeValue] = dict(
        parse_pairs(settings.resource_attributes or "", "OTEL_RESOURCE_ATTRIBUTES")
    )
    attributes[SERVICE_NAME] = settings.service_name or service_name
    if service_version is not None:
        attributes[SERVICE_VERSION] = service_version
    headers = settings.exporter_otlp_headers
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.exporter_otlp_endpoint.rstrip('/')}/v1/traces",
        headers=(
            parse_pairs(headers.get_secret_value(), "OTEL_EXPORTER_OTLP_HEADERS")
            if headers is not None
            else None
        ),
        compression=Compression.Gzip,
    )
    provider = TracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


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
