"""The pipeline's metrics (brief §7), defined in one place so their names, units, and attributes
are easy to review.

Instruments are created here, at import time, through the OpenTelemetry API. Until
`telemetry.setup_telemetry` installs a meter provider they record nothing; with telemetry off they
never do. Values are exported over OTLP/HTTP every minute (see `telemetry`).

Attributes must have few possible values, because each combination becomes its own time series
(Grafana Cloud's free tier allows 10,000 active series; ADR-0016 has the estimate). Form types,
channels, outcomes, providers, topics, and partitions are fine. Accession numbers, tickers, CIKs,
URLs, email addresses, and webhook targets never are.

Counters that matter for alerts start at zero (`start_at_zero`) as soon as a process knows which
attribute values it will use. Prometheus computes increases from consecutive values, so a series
that first appears at 1 would hide its first event.

Prometheus names: dots become underscores, counters get `_total`, and a unit is appended, so
`filings.sec.rate_limit.wait` (seconds) is `filings_sec_rate_limit_wait_seconds`.
"""

import threading
from collections.abc import Iterable, Mapping

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Counter, Observation
from opentelemetry.util.types import AttributeValue

from filings_common import telemetry

meter = metrics.get_meter("filings")

# Attributes besides the ones in `telemetry`.
ALERT_CHANNEL = "filings.alert.channel"
STATUS_CLASS = "filings.status_class"


class LatestValues:
    """The latest measured values behind an observable gauge, by owner.

    The code that measures them (a consume loop) replaces its own values; the meter reads them
    all when it exports. An owner that stops removes its values, so their series end instead of
    repeating a stale value.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[object, list[Observation]] = {}

    def replace(
        self, owner: object, values: Iterable[tuple[int, Mapping[str, AttributeValue]]]
    ) -> None:
        observations = [Observation(value, dict(attributes)) for value, attributes in values]
        with self._lock:
            self._values[owner] = observations

    def remove(self, owner: object) -> None:
        with self._lock:
            self._values.pop(owner, None)

    def observe(self, options: CallbackOptions) -> Iterable[Observation]:
        with self._lock:
            return [o for observations in self._values.values() for o in observations]


def start_at_zero(counter: Counter, attribute_sets: Iterable[Mapping[str, AttributeValue]]) -> None:
    """Create `counter`'s series for each attribute set, at zero."""
    for attributes in attribute_sets:
        counter.add(0, dict(attributes))


def status_class(status_code: int) -> str:
    """`404` -> `4xx`: one series per class of HTTP status instead of one per status."""
    return f"{status_code // 100}xx"


# --- Pipeline throughput (by filings.form_type) ---------------------------------------------------

FILINGS_DISCOVERED = meter.create_counter(
    "filings.discovered",
    unit="{filing}",
    description="New filings recorded by discovery.",
)
FILINGS_PARSED = meter.create_counter(
    "filings.parsed",
    unit="{filing}",
    description="Filings parsed and announced for the first time (status discovered -> parsed).",
)
FILINGS_READY = meter.create_counter(
    "filings.ready",
    unit="{filing}",
    description="Filings summarized and announced for the first time (status parsed -> ready).",
)

# --- Alerts ---------------------------------------------------------------------------------------

NOTIFICATIONS = meter.create_counter(
    "filings.notifications",
    unit="{notification}",
    description=(
        "Alert deliveries, by channel and outcome: sent, failed (for good), or retry (a transient "
        "failure; the alert is tried again)."
    ),
)
NOTIFICATION_OUTCOMES = ("sent", "failed", "retry")

# --- Kafka consumers (labelled with the semantic conventions' messaging.* attributes) -------------

DLQ_MESSAGES = meter.create_counter(
    "filings.dlq.messages",
    unit="{message}",
    description="Messages sent to a dead-letter topic (messaging.destination.name).",
)
DLQ_DEPTH_VALUES = LatestValues()
DLQ_DEPTH = meter.create_observable_gauge(
    "filings.dlq.depth",
    callbacks=[DLQ_DEPTH_VALUES.observe],
    unit="{message}",
    description=(
        "Messages kept in a dead-letter topic (messaging.destination.name), measured by its "
        "source topic's consumers. Nothing consumes dead-letter topics: they hold every dead "
        "letter until an operator trims them."
    ),
)
CONSUMER_LAG_VALUES = LatestValues()
CONSUMER_LAG = meter.create_observable_gauge(
    "filings.consumer.lag",
    callbacks=[CONSUMER_LAG_VALUES.observe],
    unit="{message}",
    description=(
        "Messages in a partition that its consumer group hasn't reached yet: the partition's "
        "high watermark minus the consumer's position."
    ),
)

# --- LLM summaries (labelled with gen_ai.provider.name) -------------------------------------------

LLM_DURATION = meter.create_histogram(
    "filings.llm.duration",
    unit="s",
    description=(
        "Time per LLM provider call, by provider and outcome (ok, invalid, error, rate_limited), "
        "including the validation of its answer."
    ),
    # Gemini answers in seconds; a small local model on CPU takes one to three minutes.
    explicit_bucket_boundaries_advisory=[0.5, 1, 2.5, 5, 10, 20, 30, 60, 90, 120, 180, 300],
)
LLM_FAILOVERS = meter.create_counter(
    "filings.llm.failovers",
    unit="{answer}",
    description=(
        "Answers from a fallback provider (gen_ai.provider.name), because the providers before it "
        "failed or were skipped with their circuit open."
    ),
)
LLM_BREAKER_OPENS = meter.create_counter(
    "filings.llm.circuit_breaker.opens",
    unit="{open}",
    description="Times a provider's circuit breaker opened, so it is skipped for a while.",
)

# --- SEC EDGAR (discovery is a short-lived job: one series per run) -------------------------------

SEC_RATE_LIMIT_WAIT = meter.create_histogram(
    "filings.sec.rate_limit.wait",
    unit="s",
    description="Time each SEC request attempt waited for the shared rate limiter.",
    # Zero is its own bucket: most requests shouldn't wait at all.
    explicit_bucket_boundaries_advisory=[0, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
)
SEC_RESPONSES = meter.create_counter(
    "filings.sec.responses",
    unit="{response}",
    description=(
        "SEC responses, retries included, by status class (2xx, 4xx, ...; error when no response "
        "arrived)."
    ),
)
SEC_THROTTLED = meter.create_counter(
    "filings.sec.throttled",
    unit="{response}",
    description="SEC 429 (Too Many Requests) responses: the rate limit is set too high.",
)

# Attribute sets, for code that records with them.


def form_type(value: str) -> dict[str, AttributeValue]:
    return {telemetry.FORM_TYPE: value}


def topic(name: str) -> dict[str, AttributeValue]:
    return {telemetry.MESSAGING_DESTINATION: name}
