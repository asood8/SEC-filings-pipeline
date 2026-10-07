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

The metrics from brief §7, dashboards, and alerts are the second half of Phase 6 (see Metrics).

## Decision

### Libraries and setup

- **OpenTelemetry**: the API and SDK, an **OTLP over HTTP/protobuf** exporter, and FastAPI's
  instrumentation for the API's request spans. No other auto-instrumentation.
- **One setup call per process**, `filings_common.telemetry.setup_telemetry(service_name)`. It
  configures logging and, when an endpoint is set, a tracer provider with a batching span
  processor and gzip compression. The CLIs shut it down on exit (after their SIGTERM handling),
  which exports the spans still buffered.
- **Standard configuration variables**, read through a settings class so they also work from
  `.env`:
  - `OTEL_EXPORTER_OTLP_ENDPOINT`: base URL; spans go to `<endpoint>/v1/traces`. **Unset means
    tracing is off**: the OpenTelemetry API's built-in no-op tracer stays in place, nothing is
    exported, and the SDK and exporter aren't even imported.
  - `OTEL_EXPORTER_OTLP_HEADERS`: Grafana Cloud's `Authorization` header. A secret: it is never
    logged (the startup log names only the endpoint's host), and errors parsing it never quote it.
  - `OTEL_SERVICE_NAME` (replaces the service's own name), `OTEL_RESOURCE_ATTRIBUTES` (such as
    `deployment.environment.name`), and `OTEL_SDK_DISABLED`. The SDK's own variables still apply,
    such as `OTEL_TRACES_SAMPLER` (default: keep every trace).
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
| API | `<METHOD> <route>` (SERVER), except `/healthz` | method, route, URL with query string, status, user agent, client address |

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
- **Memory when on:** measured at about 15 MB more per Python process (SDK, exporter, protobuf),
  so roughly 90 MB for six processes, inside the services' 1 GB budget line. With tracing off a
  process uses about 5 MB less than with it on. (redis-py 8 imports part of the SDK on its own
  when the SDK is installed, for its built-in metrics.)
- **Trace volume is tiny for the free tier:** a filing's trace is a few dozen spans; at tens of
  filings a day that's megabytes a month against 50 GB. API request spans will dominate; health
  checks are excluded. If volume ever grows, parent-based ratio sampling keeps whole traces,
  because the sampling decision travels in `traceparent` through the outbox and Kafka.
- **Tracing never blocks the pipeline.** Spans are exported in the background; if the endpoint is
  down, they are dropped. Only shutdown waits for the last export: each attempt gives up after the
  exporter's timeout (10 s by default), and the whole flush after 30 s. Phase 7's pods need a grace
  period that allows for it.
- **Traces show waiting.** Gaps between stages are real time: the relay's polling interval and
  consumer lag. A long `process` span means retries (it includes their backoff).
- **The OTLP exporter brings `requests`** as a transitive dependency for its own HTTP. Our code
  never uses it; ADR-0005 still governs the HTTP the services make.
- **A migration** adds `outbox.trace_context`. The relay reads the column, so apply the migration
  before running the new code; older code ignores it.
- **Follow-ups:** metrics, dashboards, alerts, and the local viewer (Metrics, below); the
  production headers secret (a Sealed Secret) and whether to export straight to Grafana Cloud or
  through an in-cluster collector (the brief's budget reserves ~0.2 GB for one) in Phase 7.

## Metrics

_To be written with the metrics work (Phase 6, part 2): the metrics from brief §7 and how they are
exported, dashboards and alerts in Grafana Cloud, and the optional local viewer
(grafana/otel-lgtm in a Compose profile)._

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
