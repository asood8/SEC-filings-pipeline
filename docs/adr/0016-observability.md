# ADR-0016: Observability with OpenTelemetry

- **Status:** Accepted
- **Date:** 2026-10-07

## Context

Phase 6 is done when "one trace follows a filing from discovery to alert". A filing passes through
five processes (discovery, the outbox relay, the parser, the summarizer, the notifier). Between
any two stages it crosses two hand-offs: a row in the Postgres outbox, then a Kafka message.

- **Nothing carries trace context through those hand-offs by itself.** Tracing libraries pass
  context along HTTP calls automatically, but no library knows our outbox table. The relay is a
  separate process that runs later and can't tell which span wrote a row.
- **httpx2 has no auto-instrumentation** (it's a fork; ADR-0005), and confluent-kafka's
  instrumentation knows nothing about the outbox or our retry and dead-letter loop.
- **Traces leave the VM.** In production they go to Grafana Cloud, a third party. Some of what the
  services handle must never go there: webhook URLs (anyone holding one can post to the channel),
  email addresses, ID and session tokens, API keys, and LLM prompt and answer text.
- **$0 and 12 GB.** Grafana Cloud's free tier stores 50 GB of traces for 14 days (brief §9). The
  services' share of the VM's memory budget is about 1 GB (brief §7).

The metrics from brief §7, dashboards, alerts, and a local viewer are the second half of Phase 6
(see Metrics). Grafana Cloud's free tier allows 10,000 active metric series.

## Decision

### Libraries and setup

- **OpenTelemetry**: the API and SDK, an **OTLP over HTTP/protobuf** exporter, and FastAPI's
  instrumentation for the API's request spans and metrics. No other auto-instrumentation.
- **One setup call per process**, `filings_common.telemetry.setup_telemetry(service_name)`. It
  configures logging and, when an endpoint is set, a tracer provider with a batching span
  processor and gzip compression, and a meter provider (see Metrics). The CLIs shut it down on
  exit (after their SIGTERM handling), which exports the spans still buffered and the metrics'
  final values.
- **Standard configuration variables**, read through a settings class so they also work from
  `.env`:
  - `OTEL_EXPORTER_OTLP_ENDPOINT`: base URL; spans go to `<endpoint>/v1/traces` and metrics to
    `<endpoint>/v1/metrics`. **Unset means telemetry is off**: the OpenTelemetry API's built-in
    no-op tracer and meter stay in place, nothing is exported, and the SDK and exporter aren't
    even imported.
  - `OTEL_EXPORTER_OTLP_HEADERS`: Grafana Cloud's `Authorization` header. A secret: it is never
    logged (the startup log names only the endpoint's host), and errors parsing it never quote it.
  - `OTEL_SERVICE_NAME` (replaces the service's own name), `OTEL_RESOURCE_ATTRIBUTES` (such as
    `deployment.environment.name`), and `OTEL_SDK_DISABLED`. The SDK's own variables still apply,
    such as `OTEL_TRACES_SAMPLER` (default: keep every trace).
  - `OTEL_TRACES_EXPORTER` and `OTEL_METRICS_EXPORTER` (`otlp` or `none`) turn off one signal,
    and `OTEL_METRIC_EXPORT_INTERVAL` sets the milliseconds between metric exports (60000).
- **Log correlation:** while a span is active, each log line ends with `trace_id=… span_id=…`, so
  a trace's log lines can be found from the trace and vice versa.

### One trace per filing

Context travels in the W3C Trace Context format (`traceparent`, `tracestate`):

1. **Discovery** starts a trace per poll, with a span per company. Each new filing starts **a new
   trace of its own** (`discover filing`), linked to the company poll that found it. Its SEC
   requests, object-store writes, and the transaction recording it (`record filing`) are its
   children.
2. **Outbox:** `enqueue` stores the current span's context in a new nullable `outbox.trace_context`
   column, in the same transaction as the event.
3. **Relay:** each row is published under a PRODUCER span (`send <topic>`) that is a **child** of
   the stored context. That span's context goes in the Kafka message headers.
4. **Consume loop:** each message is handled in a CONSUMER span (`process <topic>`) that continues
   the headers' context. It carries `messaging.system=kafka`, the topic, partition, offset, and
   consumer group. Retries are exception events on it, and dead-lettered messages keep their trace
   headers.
5. The handler's own `enqueue` runs inside that span, so the next event continues the same trace
   automatically, through to the notifier.

`filings-replay` does the same as discovery: each replayed filing starts its own trace, linked to
the replay run. Rows written outside any trace (for instance with tracing off) start a new trace at
the relay. A process with tracing off still forwards the context it received: the no-op tracer
passes it through unchanged, so one untraced stage doesn't break the chain.

**Why a child at the relay and consumer, not a link:** publishing and handling are steps of the
filing's work, not separate units of work. Links would split each filing into one trace per stage,
which is the opposite of the goal. **Why a new root per filing, with a link:** a poll covers 35
companies and can find dozens of filings. As the poll's children, every filing's whole journey
would hang off one poll trace hours long, which is hard to read or search. The link still leads
from a filing to the poll that found it.

### Manual spans

| Where | Span | Attributes (besides the filing's accession number, form type, CIK) |
|---|---|---|
| EDGAR client | `GET` (CLIENT), one per request including retries | URL (public), status, resend count, total rate-limiter wait; a `retry` event per retry |
| Object store | `object_store put` / `get` | backend, key (public paths) |
| MongoDB | `replace_one` / `find_one parsed_filings` | database, collection, operation |
| Parser | `parse` | parser version |
| Summarizer | `summarize` | provider, model, prompt version, count of ungrounded numbers |
| LLM failover | `chat <model>` (CLIENT), one per provider tried | `gen_ai.*`: provider, model, token counts; outcome (`ok`, `invalid`, `error`, `rate_limited`); Gemini's rate-limiter wait |
| Embeddings | `embeddings <model>` (CLIENT) | model, number of inputs |
| Notifier | `deliver alert`, one per subscription | channel and outcome (`sent`, `failed`, `retry`, `already_settled`) only |
| API | `<METHOD> <route>` (SERVER), except `/healthz` and `/readyz` | method, route, URL with query string, status, user agent, client address |

Accession numbers are fine on spans (they are public, and the way to find a filing's trace), but
must never become metric labels: each is unique.

### Privacy

No span attribute, span event, or log line may hold a webhook URL, an email address, an ID or
session token, an API key, or LLM prompt or answer text.

- Spans around work whose errors may quote such data (LLM and embedding calls, deliveries, the
  consume loop) record exceptions **by type only**, with no message or stack trace. The log line,
  which carries the trace ID, has the details where they are safe to log.
- The API records no request or response headers or bodies. Should header capture ever be turned on
  (`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`), cookie and authorization values are still
  redacted. Behind Cloudflare Tunnel, the client address is the tunnel connector's, not the
  visitor's.
- An SMTP "recipient refused" error used to quote the address; it now gives only the SMTP code.
- Tests check this: delivery spans, LLM and embedding spans, and sign-in requests are searched for
  the private values they handled.

## Alternatives considered

- **Zero-code auto-instrumentation** (`opentelemetry-instrument`). It patches every library it
  finds (psycopg, Redis, pymongo, and more): a span per SQL query or Redis call, dozens per filing,
  more memory and data, and implicit behavior that makes the privacy rule harder to guarantee.
  Explicit spans at the boundaries that matter answer "where did this filing spend its time".
  Database or Redis instrumentation can be added later if a question needs it.
- **confluent-kafka instrumentation.** It traces produce and poll calls, but knows nothing of the
  outbox, so the trace would still break at the relay, and its spans don't cover a message's
  retries and dead-lettering. The consume loop is the natural boundary, and it is a few lines.
- **OTLP over gRPC.** It needs grpcio, a large native dependency. HTTP/protobuf is accepted by
  Grafana Cloud and every collector.
- **Trace context inside the event payload** instead of a column and headers. It would put
  observability into the events' schema (a contract between stages, with versions), and replayed
  events would carry stale contexts. Headers are the standard place; the column is the outbox's
  equivalent of headers.
- **Starting traces at the relay** (no column). Each filing's trace would miss discovery, and every
  later stage would start a fresh trace, since the relay can't know which consumer span wrote a
  row.
- **A self-hosted trace backend (Jaeger, Tempo) on the VM.** It costs memory the VM doesn't have
  to spare; Grafana Cloud's free tier covers our volume.

## Consequences

- **Off by default and cheap when off.** Without an endpoint there's no exporter and no network
  traffic, and nothing to configure locally.
- **Memory when on:** about 13 to 15 MB more per Python process (SDK, exporters, protobuf), so
  roughly 90 MB for six processes, inside the services' 1 GB budget line. A process with the
  services' libraries peaked at 75 MB with telemetry off, 88 MB with traces only, and 88 MB with
  traces and metrics: metrics add nothing measurable on top of tracing. (redis-py 8 imports part
  of the SDK on its own when the SDK is installed, for built-in metrics that stay off unless
  enabled.)
- **Trace volume is tiny for the free tier:** a filing's trace is a few dozen spans; at tens of
  filings a day that's megabytes a month against 50 GB. API request spans will dominate; health
  checks are excluded. If volume ever grows, parent-based ratio sampling keeps whole traces,
  because the sampling decision travels in `traceparent` through the outbox and Kafka.
- **Tracing never blocks the pipeline.** Spans are exported in the background; if the endpoint is
  down, they are dropped. Only shutdown waits for the last export: each attempt gives up after the
  exporter's timeout (10 s by default), and the whole flush after 30 s. Phase 7's pods need a grace
  period that allows for it: 45 s on every pod that exports, since 2026-10-07 (ADR-0017).
- **Traces show waiting.** Gaps between stages are real time: the relay's polling interval and
  consumer lag. A long `process` span means retries (it includes their backoff).
- **The OTLP exporter brings `requests`** as a transitive dependency for its own HTTP. Our code
  never uses it; ADR-0005 still governs the HTTP the services make.
- **A migration** adds `outbox.trace_context`. The relay reads the column, so apply the migration
  before running the new code; older code ignores it.
- **Metrics are measured where the work happens.** No exporter or scraper runs beside the
  services. The price: a stopped consumer reports no lag at all (hence the "not reporting" rule),
  and lag pauses while a handler is busy.
- **Dashboards and alert rules are tested against the code**, so renaming a metric or attribute
  fails CI instead of silently emptying a panel.
- **Alerts notify nobody yet.** Locally they show in Grafana only; Grafana Cloud needs a contact
  point and notification policy (Phase 7).
- **Follow-ups (Phase 7):** the production headers secret (a Sealed Secret); Terraform for the
  dashboards (`grafana_dashboard`) and rules (`grafana_rule_group`, substituting the data source
  UID), plus a contact point (Discord, its webhook a secret) and notification policy; the pods'
  `service.instance.id` (the pod name) and a shutdown grace period for the last export (done
  2026-10-07: 45 s, and discovery now stops on SIGTERM so a run killed at its deadline still
  exports its spans; ADR-0017); and whether to export straight to Grafana Cloud or through an
  in-cluster collector (the brief's budget reserves ~0.2 GB for one).

## Metrics

Phase 6's second half: the metrics from brief §7, dashboards and alert rules as code, and an
optional local viewer.

### What is measured

All of the pipeline's instruments are defined in one module, `filings_common.metrics`, so their
names, units, and attributes can be reviewed in one place. The Prometheus name (how Prometheus and
Grafana Cloud store a metric) replaces dots with underscores, adds `_total` to counters, and adds
the unit, so `filings.sec.rate_limit.wait` becomes `filings_sec_rate_limit_wait_seconds`.

| Metric | Kind | Attributes | Recorded |
|---|---|---|---|
| `filings.discovered` | counter | form type | by discovery, for each new filing it records |
| `filings.parsed`, `filings.ready` | counter | form type | when a filing first moves to `parsed` / `ready` |
| `filings.outbox.published` | counter | topic | by the relay, once rows are marked published |
| `filings.notifications` | counter | channel; outcome `sent`, `failed`, `retry` | by the notifier, after its transaction commits |
| `filings.dlq.messages` | counter | dead-letter topic | by the consume loop, for each dead letter |
| `filings.dlq.depth` | gauge | dead-letter topic | messages the topic holds, read from the broker |
| `filings.consumer.lag` | gauge | consumer group, topic, partition | a partition's high watermark minus the group's position |
| `filings.llm.duration` | histogram, s | provider; outcome `ok`, `invalid`, `error`, `rate_limited` | each provider call, validating the answer included |
| `filings.llm.failovers` | counter | the provider that answered | answers that came from a fallback provider |
| `filings.llm.circuit_breaker.opens` | counter | provider | each time a provider's breaker opens |
| `filings.sec.rate_limit.wait` | histogram, s | none | each SEC request attempt's wait for the shared limiter |
| `filings.sec.responses` | counter | status class (`2xx`, `4xx`, `5xx`, `error`) | each SEC attempt, retries included |
| `filings.sec.throttled` | counter | none | SEC 429 responses |
| `http.server.request.duration` | histogram, s | method, route, status | FastAPI's instrumentation, except `/healthz` and `/readyz` |
| `http.server.active_requests` | up-down counter | method, scheme | FastAPI's instrumentation |

The brief's "per hour" figures and the notification success rate (sent / (sent + failed)) are
computed by the dashboards from these counters rather than recorded separately.

### How they are exported

- The same `setup_telemetry` call adds a **meter provider** whose reader sends every metric's
  current value over OTLP/HTTP (gzip) to `<endpoint>/v1/metrics` once a minute
  (`OTEL_METRIC_EXPORT_INTERVAL`), and once more when the process shuts down. Metrics are off
  with traces when no endpoint is set; `OTEL_TRACES_EXPORTER=none` or `OTEL_METRICS_EXPORTER=none`
  turns off just one of them.
- **Cumulative values:** each export carries totals since the process started. That is what
  Prometheus and Grafana Cloud store; queries turn totals into rates, and handle the reset to zero
  when a process restarts.
- **Each process is its own `service.instance.id`**: a random ID, or in Kubernetes the pod name
  from `OTEL_RESOURCE_ATTRIBUTES`. Two replicas, or two runs of a job, writing their totals into
  one series would turn it into nonsense.
- Instruments are created when the module is imported, through the OpenTelemetry API. They record
  nothing until `setup_telemetry` installs the SDK, and with telemetry off they never do.

### Decisions in the details

- **Counted once, after the commit.** `filings.parsed` and `filings.ready` count only the
  transaction that moves a filing's status, so replays and redeliveries never count twice and the
  counts match the database. Deliveries are counted after their transaction commits, and an alert
  that turns out to be settled already isn't counted.
- **Counters start at zero.** Prometheus computes an increase from consecutive values, so a series
  whose first value is 1 hides that first event, and a "nothing happened" alert has no series to
  look at. Each process creates the series it knows it will use, at 0, when it starts: per form
  type, per channel and outcome, per topic.
- **Consumer lag is measured by the consumers themselves**, at most every 30 seconds, between
  messages: for each partition assigned to the consumer, the high watermark (the next offset to be
  written) minus the consumer's position, or minus its group's committed offset before it has read
  anything. Each consumer reports only its own partitions; queries take the maximum per partition
  (during a rebalance two consumers can briefly report the same one), then add them up per group.
  While a handler works (a slow LLM call, retry backoff), the gauge keeps its last value.
- **"DLQ not empty" is measured from the broker:** a dead-letter topic's high minus low
  watermarks, read by the consumers of its source topic. Nothing consumes dead-letter topics, so
  this is what they hold, which is what the alert is about; `filings.dlq.messages` shows when
  dead letters arrived. Replay works from Postgres, not from the dead-letter topics, so after
  fixing the cause and replaying, an operator empties the topic
  (`rpk topic trim-prefix <topic> --offset end`). Dead-letter topics are kept indefinitely
  (`retention.ms=-1`, like the pipeline topics), so nothing removes them otherwise.
- **Short-lived jobs.** Discovery and replay run for seconds to minutes. Each run has its own
  series, usually holding a single value: the run's total, exported on the way out. `rate()` and
  `increase()` need two values per series, so the dashboards and rules add up a job's counters
  with `max_over_time(...[window])` (each run's latest total in the window), summed over runs. A
  test checks that every query on a job metric does this. (A Pushgateway would be another
  component, and making discovery a long-running loop would change how it is deployed, for a
  query detail.)
- **The API's HTTP metrics follow the stable HTTP semantic conventions**
  (`OTEL_SEMCONV_STABILITY_OPT_IN=http`, set by the API itself). The instrumentation's older
  default has no route attribute but does record the request's Host header, which any client can
  set to anything: a loop of requests with random hosts would create a series each. Request and
  response size histograms are dropped by an SDK view; they aren't worth 17 series per route and
  status.
- **Buckets per histogram:** LLM calls from 0.5 s to 5 minutes (Gemini answers in seconds, a 3B
  model on CPU in one to three minutes); SEC limiter waits from 0, its own bucket since most
  requests shouldn't wait at all, to 30 s.
- **Few values per attribute.** Form types, topics, partitions, consumer groups, channels,
  outcomes, providers, and status classes have a handful of values each. Accession numbers, CIKs,
  tickers, URLs, email addresses, and webhook targets are never attributes; tests check that no
  accession number or CIK becomes one.

### Series budget

Grafana Cloud's free tier allows **10,000 active series**. Each combination of attribute values
is one series, and a histogram is one series per bucket plus `_sum` and `_count` (17 for the API's
request duration). Counted in the local viewer after a run that exercised every metric (see
Verification), per process:

| Process | Series | Most of them |
|---|---|---|
| API | 274 | request duration: 16 route and status combinations × 17 |
| Summarizer | 40 | LLM duration: 2 provider and outcome combinations × 15 |
| Discovery, per run | 20 | SEC limiter wait histogram (13) |
| Notifier | 15 | 3 channels × 3 outcomes |
| Parser | 8 | lag for 3 partitions, 2 form types |
| Outbox relay | 4 | 3 topics |

Each includes the process's `target_info` series (its resource attributes). Worse cases: the API
seeing every route with three or four status codes (about 60 combinations, roughly 1,000 series),
and the summarizer seeing all 8 provider and outcome combinations (about 135). A job run's series
stay active for a while after its single export, so with a run every 10 minutes a few runs
overlap (about 100), and each pod restart briefly doubles that pod's series. Production should
therefore sit between **about 400 and 1,500 active series, 4 to 15% of the free tier**. The
headroom is for mistakes: a high-cardinality attribute is what would eat it. Grafana Cloud's
Application Observability, if turned on, generates its own series from traces (span metrics), so
count those before enabling it.

### Dashboards

Two dashboards, as JSON in `deploy/grafana/dashboards/`:

- **SEC Filings: Pipeline**: filings per hour by stage and by form type; consumer lag by group and
  by partition; events published and dead letters (held, and per hour); LLM latency (p50 and p95 of
  successful calls), calls by provider and outcome, failovers and breaker opens; SEC limiter wait
  and responses by status class, with 429s; alerts by channel and outcome with the success rate;
  and the latest filing traces from Tempo.
- **SEC Filings: API**: requests, p95 latency, server-error ratio, and 429s over the selected
  range; requests by route; responses by status class; p50/p95/p99 latency, p95 by route, and the
  slowest routes; requests in progress; and traces of slow requests.

Every panel has a description saying what it shows and how to read it. The data sources are
dashboard variables (`${datasource}`, `${tempo}`) that pick the first Prometheus and Tempo data
source, so the same files work locally and in Grafana Cloud, whose data sources have other UIDs.
Locally, Grafana loads them through a file provider (`deploy/grafana/local/dashboards.yaml`) and
reloads them within seconds of a change; edits made in Grafana can't be saved over them, so change
the JSON (or export an edited copy into it). In Phase 7 Terraform's Grafana provider uploads the
same files (`grafana_dashboard` with `config_json = file(...)`).

`tests/test_grafana_provisioning.py` checks every PromQL query in the dashboards and alert rules
against the names the SDK actually exports for the instruments in `filings_common.metrics` (plus
FastAPI's metrics): a renamed metric or attribute would otherwise only show up as an empty panel,
or an alert that never fires. It also checks that every metric is on a dashboard, that job metrics
are added up with `max_over_time`, that panels don't overlap, and that the files stay portable.

### Alerts

Four rules, in `deploy/grafana/alerting/filings.yaml`:

| Rule | Fires when | After |
|---|---|---|
| No new filings in 24 hours | nothing was discovered, or nothing was summarized, in the last 24 h; checked from Tuesday 12:00 to Saturday 12:00 UTC, so each window covers at least 14 of EDGAR's weekday filing hours (6:00 to 22:00 Eastern) | 15 min |
| Dead-letter topic not empty | a dead-letter topic holds a message | at once |
| Consumer lag high | a group is more than 100 messages behind on a topic | 15 min |
| Pipeline service not reporting | a long-running service exported metrics in the last day, but not in the last 10 minutes | at once |

The fourth (critical; the others are warnings) is not in the brief, but the other rules can't see
a stopped consumer: its lag and dead-letter gauges simply disappear, and "no data" counts as fine.
US market holidays will fire the first rule; for a few days a year, that is cheaper than a holiday
calendar. A replay of many 8-Ks through the local model can fire the lag rule, which is correct.

**Why Grafana-managed rules in Grafana's provisioning format**, rather than Prometheus-style rule
files:

- **One file works in both places.** The local viewer's Grafana loads it at startup (its
  Prometheus has no ruler or Alertmanager set up), and Terraform's Grafana provider creates the
  same rules in Grafana Cloud (`grafana_rule_group`), the provider that also uploads the
  dashboards. Prometheus-style rules in Grafana Cloud live in its Mimir ruler, which needs another
  tool (mimirtool) or Terraform provider.
- **They notify through Grafana's contact points and notification policies**, where the alert
  channel (Discord) gets configured, and each rule links to its dashboard panel.
- **The way out stays open:** the format is Grafana's, but the queries are plain PromQL, so moving
  to Prometheus rules means copying the `expr` fields.

Portability details: the only environment-specific value is the Prometheus data source UID
(`prometheus` locally; Terraform substitutes Grafana Cloud's). Annotations use
`{{ .Labels.x }}` rather than `{{ $labels.x }}`, because Grafana expands `$name` in provisioning
files as an environment variable. Contact points and notification policies are not in the file:
they hold a secret webhook URL and differ per environment.

### Local viewer

`make obs-up` starts **grafana/otel-lgtm** (pinned to 0.35.0, the current release) in the Compose
profile `observability`, like the local LLM: `make up` doesn't start it, `make down` stops it, and
`make ps` lists it. The user chose this on 2026-10-07 (brief §12). One container runs an
OpenTelemetry Collector that receives OTLP on port 4318, Prometheus, Tempo, Loki, and Grafana on
http://localhost:3001 (the website's dev server has 3000), which loads our dashboards and alert
rules. Every port is bound to 127.0.0.1, since its Grafana lets anyone in as an admin. Besides
Grafana and OTLP, Prometheus's port 9090 is published, to try queries directly. It keeps its data
in a named volume and has a 1.5 GB memory limit: in the verification its processes used about
0.55 GB, and file cache filled the rest, reclaimed at the limit without any process being killed.
Usage reporting (Grafana, Tempo, Loki, Pyroscope), Grafana's update checks, news feed, and plugin
downloads are all turned off, so nothing in it phones home.

The services export to it when `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` (see
`.env.example`; `OTEL_METRIC_EXPORT_INTERVAL=10000` makes panels move faster while watching).
Production uses Grafana Cloud: the VM can't spare a gigabyte for this.

### Verification

On 2026-10-07 the real stage code ran as separate processes (each under its service name) against
the local viewer: a scratch Postgres and MongoDB database, its own Redpanda topics and consumer
groups, a fake SEC (MockTransport, with one 429 and one 503 per run), a fake LLM behind a "local"
provider that was down (so every 8-K failed over), and fake alert channels (one flaky, one
rejecting). Three discovery runs found 20 filings, then nothing; a malformed message was sent to
the parser's topic; the API served a mix of requests, including 404s, 401s, and 429s. Then:

- **Traces:** Tempo had 20 filing traces, one per filing, each with a single root
  (`discover filing`) and spans from all five pipeline services: discovery, relay, parser,
  summarizer, and notifier. The 11 filings with a subscriber end in `deliver alert` spans.
- **Metrics:** Prometheus had every metric in the table under its expected name, with the expected
  values (20 discovered: 12 Form 4s and 8 8-Ks; failovers to the fallback provider; 429s from SEC;
  deliveries sent, retried, and failed by channel).
- **Dashboards:** both loaded from files, and every Prometheus query on them returned data. The
  only empty panel was "Slow requests", since no request took over 250 ms.
- **Alerts:** "Dead-letter topic not empty" fired for the malformed message, and resolved within a
  minute of trimming the topic. "Pipeline service not reporting" fired 10 minutes after the
  services stopped, once per service. The other rules evaluated without errors and stayed normal.

## Tools in plain language

- **OpenTelemetry (OTel):** an open standard, with libraries, for telemetry: traces, metrics, and
  logs. Code is written against its API; the SDK decides where the data goes, so changing
  backends is a configuration change.
- **Traces and spans:** a span is one timed operation (an SEC request, an LLM call, handling one
  Kafka message) with attributes and a status. Spans nest, each with a parent, and a trace is the
  tree of spans for one piece of work: here, one filing's journey. A link connects spans in
  different traces without making one the other's parent ("this filing was found by that poll").
- **Context propagation:** passing the current trace and span IDs along with the work, so the next
  process's spans join the same trace. Over HTTP it's a header; here it's a Kafka header and an
  outbox column. W3C Trace Context is the standard format, a `traceparent` value like
  `00-<trace id>-<span id>-01`.
- **Why the outbox stores trace context:** the outbox separates writing an event from publishing
  it. The relay publishes later, in another process, and can't see the span that wrote the row, so
  the row itself must carry the context, like a header on a letter.
- **OTLP:** the OpenTelemetry Protocol, the standard format for sending telemetry. Over HTTP with
  protobuf bodies, Grafana Cloud, collectors, and local viewers all accept it.
- **Batch span processor:** finished spans wait in memory and are sent in batches by a background
  thread, so tracing never slows a request or the pipeline.
- **Metrics:** numbers measured over time, cheap to store and query in aggregate ("filings per
  hour", "95th percentile latency"). Traces describe single pieces of work in detail; metrics
  describe all of them in summary, which is what dashboards and alerts need.
- **Counter, gauge, histogram:** a counter only goes up (filings discovered so far); a gauge is a
  current value that goes up and down (consumer lag); an up-down counter is a gauge kept by adding
  and subtracting (requests in progress); a histogram counts values into ranges called buckets
  (LLM calls under 5 s, under 10 s, ...), from which percentiles such as p95 are estimated.
- **Time series, labels, and cardinality:** a time series is one metric for one combination of
  label values, like the lag of group `parser` on partition 2. (OpenTelemetry calls labels
  attributes.) Cardinality is how many series a metric has; it multiplies across labels, which is
  why a label that is unique per filing, like the accession number, is forbidden.
- **Cumulative temporality:** each export sends running totals since the process started, rather
  than what happened since the previous export, so a lost export loses nothing.
- **Prometheus and PromQL:** Prometheus is a time-series database; PromQL is its query language.
  `rate()` and `increase()` turn a counter's running total into a per-second rate or an increase
  over a window, `max_over_time()` takes a series' highest value in a window, and
  `histogram_quantile()` estimates a percentile from histogram buckets.
- **The LGTM stack:** Grafana Labs' open-source tools: **L**oki for logs, **G**rafana for
  dashboards and alerting, **T**empo for traces, and **M**imir for metrics (a horizontally scaled
  Prometheus). Grafana Cloud runs them as a service. grafana/otel-lgtm packs them into one
  container for development, with plain Prometheus standing in for Mimir.
- **OpenTelemetry Collector:** a service that receives telemetry (OTLP, among others), can filter
  or enrich it, and forwards it to backends. In the local viewer it receives what the services send
  and passes metrics to Prometheus and traces to Tempo.
- **Grafana alerting:** Grafana runs each rule's query on a schedule (every minute here). A result
  past the threshold for the rule's "for" period fires an alert, and notification policies route
  it to contact points (Discord, email).
