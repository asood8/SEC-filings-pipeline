# ADR-0004: Service runtime libraries and patterns

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

Phase 1 builds the first real code: the shared EDGAR client, discovery, the transactional outbox
and its relay, and the first API endpoint. The brief fixes the broad stack (Python, FastAPI,
Pydantic, SQLAlchemy and Alembic, httpx, a Kafka client against Redpanda). This ADR records the
choices left open:

- synchronous vs asynchronous I/O;
- the specific client libraries;
- how the shared SEC rate limit works;
- the delivery guarantees of the outbox relay.

Constraints: arm64 production images, a small VM, SEC's 10 req/s limit, and idempotency on the
accession number.

## Decision

### Synchronous I/O everywhere, for now

Workers, the relay, and the API use blocking I/O; FastAPI runs plain `def` endpoints in its
thread pool.

- Discovery is deliberately throttled to 5 req/s, so async concurrency would buy nothing there.
- Kafka consumers process one message at a time, in order.
- API traffic is tiny.

Sync code is simpler to read, test, and debug, and it needs only one database driver mode.
Revisit if the API ever needs high concurrency.

### Libraries

| Need | Library | Why |
|---|---|---|
| Settings | pydantic-settings | Typed, validated config from env vars and `.env`; secrets as `SecretStr` |
| HTTP | httpx | Timeouts, redirects, and a mock transport for tests without a mocking library |
| Postgres | SQLAlchemy 2 + psycopg 3 | Typed ORM models; psycopg 3 is the maintained driver, with arm64 wheels |
| Migrations | Alembic | Autogenerates from the models. Migrations ship inside `filings_common`, so any image can run them |
| Valkey | redis-py | The standard client; Valkey speaks the same protocol |
| Kafka | confluent-kafka | Built on librdkafka: idempotent producer, mature consumer groups, typed, arm64 wheels |
| Object storage | boto3 | R2 implements the S3 API; boto3's Stubber tests it offline |
| API | FastAPI + uvicorn | As the brief specifies; OpenAPI docs come for free |

### Shared SEC rate limit

There is one token bucket in Valkey (`ratelimit:sec`), updated atomically by a Lua script that uses
Valkey's clock, so every worker agrees on time.

- **Reservation-style.** Each call takes a token immediately, even when that drives the balance
  negative, and gets back how long to sleep first. Callers queue in arrival order and nobody polls.
- **Defaults: 5 req/s, burst 1.** Settings reject rates above SEC's 10 req/s maximum. Every retry
  takes a token too.
- **Backoff.** The EDGAR client retries 429, 5xx, and network errors with jittered exponential
  backoff (1 s doubling to 60 s), and honours `Retry-After`. Other 4xx errors fail immediately.

### Outbox relay semantics

- **What a batch does.** The relay locks the oldest unpublished outbox rows with
  `FOR UPDATE SKIP LOCKED`, publishes them in id order, waits for every acknowledgement, and marks
  them published in the same transaction.
- **At-least-once delivery.** A crash between publishing and committing republishes those rows.
  Consumers must be idempotent on the accession number, which the design already requires.
- **One replica.** `SKIP LOCKED` stops two relays from publishing the same row, but only a single
  replica preserves publish order.
- **Idempotent producer.** It runs with `acks=all`, so librdkafka's internal retries never duplicate
  or reorder messages.
- **Topics.** The relay creates missing topics at startup: each pipeline topic plus its `.dlq`,
  3 partitions, replication factor 1 (one node). Retention is unlimited: events are small, and
  keeping them lets replay re-read history by resetting a consumer group.

### Integration tests

Tests that need Postgres, Valkey, or Redpanda run against the Compose stack.

- Each test run creates a throwaway Postgres database and migrates it with Alembic, which also tests
  the migrations.
- A test compares the models with the migrated schema, catching a model change with no migration.
- Locally these tests skip when the stack is down. CI starts the stack and sets
  `REQUIRE_SERVICES=1`, so there they must pass.

## Alternatives considered

- **Async throughout (asyncpg, aiokafka, httpx async).** More complex code and tests, for no gain
  under a rate limit and with tiny traffic.
- **aiokafka or kafka-python.** aiokafka is async-only. kafka-python is pure Python, slower, and has
  had long maintenance gaps.
- **A per-process rate limiter.** It cannot coordinate across workers; five workers at 5 req/s
  would send 25 req/s.
- **Mocking Postgres in tests, or using SQLite.** That would miss what matters here: `ON CONFLICT`,
  `SKIP LOCKED`, JSONB, and the real migrations.
- **Publishing to Kafka directly from discovery.** That is a dual write: a crash between the
  database commit and the publish loses the event or produces a phantom one. The outbox exists to
  prevent exactly that.

## Consequences

- Every SEC call goes through `EdgarClient`, built with `EdgarClient.from_settings`, which wires in
  the shared limiter. Code that sidesteps it would break SEC's fair-access rules.
- Each consumer still has to dedupe on the accession number; the outbox cannot guarantee
  exactly-once delivery.
- The relay must run as a single replica (a Kubernetes Deployment with `replicas: 1`).
- The local stack must be running (`make up`) for the full test suite.

## Tools in plain language

- **pydantic-settings:** reads configuration from environment variables into typed, validated
  Python objects.
- **SQLAlchemy:** maps Python classes to database tables and builds SQL safely.
- **Alembic:** versioned database schema changes ("migrations") for SQLAlchemy.
- **psycopg:** the Python driver that actually talks to Postgres.
- **confluent-kafka:** Python bindings for librdkafka, the C Kafka client most Kafka tooling builds
  on.
- **Lua script in Redis/Valkey:** a small program the server runs atomically, so a read-then-write
  can't interleave with another client's.
- **`SELECT ... FOR UPDATE SKIP LOCKED`:** a Postgres feature that locks the rows a query returns,
  and skips rows another transaction already holds instead of waiting for them.
- **uvicorn:** the web server process that runs the FastAPI application.
