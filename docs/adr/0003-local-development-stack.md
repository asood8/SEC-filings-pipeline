# ADR-0003: Local development stack

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The services need Postgres (with pgvector), Redpanda, Valkey, and MongoDB to run locally.

- The dev machine is an x86-64 laptop with 16 GB RAM, and WSL2 gets about 8 GB of that by default.
- Production is k3s on arm64. It self-hosts Postgres, Redpanda, and Valkey, but uses MongoDB Atlas
  M0 and Cloudflare R2 as managed services.
- Unit tests never call SEC.
- The local stack must be quick to start and reset, and must stay lean enough to leave room for an
  editor, a browser, and later a local LLM.

## Decision

- **Docker Compose runs the backing services** (`docker-compose.yml`, driven by `make up` /
  `make down`). During development the Python services run on the host with `uv run`, for fast
  restarts and a debugger. Container images for them come in Phase 7.
- **Use production's images where production self-hosts the service:**
  `pgvector/pgvector:0.8.7-pg18`, `redpandadata/redpanda:v26.2.3`, and `valkey/valkey:9.1.2-alpine`.
  All three publish amd64 and arm64 builds.
- **Local stand-ins for managed services:**
  - MongoDB Atlas → `mongo:8.2.12` with authentication on, like Atlas. Keep the major version
    aligned with the Atlas cluster when it is created in Phase 7.
  - Cloudflare R2 → the local-filesystem `ObjectStore` implementation (Phase 1).
  - Production LLM → Ollama on the host (Phase 2).
  - Grafana Cloud → nothing until Phase 6.
- **Kept lean:**

  | Service | Memory limit | Measured at idle | Tuning |
  |---|---|---|---|
  | Postgres | 768 MB | 40 MiB | defaults |
  | Redpanda | 1.5 GB | 193 MiB | dev-container mode, 1 core, `--memory=1G` |
  | Valkey | 256 MB | 6 MiB | `maxmemory 128mb`, no persistence |
  | MongoDB | 768 MB | 200 MiB | WiredTiger cache 0.25 GB |
  | **Total** | **3.3 GB** | **~440 MiB** | |

  Valkey keeps the default `noeviction` policy: when it is full, writes fail loudly instead of
  silently evicting rate-limit or dedupe keys. Its contents are all disposable, so it doesn't
  persist to disk.
- **Safe by default.** Ports bind to `127.0.0.1` only. Credentials come from a gitignored `.env`;
  `make up` creates it from `.env.example` on first run. They protect throwaway local containers
  only.
- **Health checks on every service.** `make up` uses `docker compose up --wait`, so it returns only
  once everything is healthy.
- **Redpanda gets two listeners.** Containers reach the broker as `redpanda:9092`; host processes
  use `localhost:19092`.
- **Compose doesn't create topics or schemas.** Topics and the database schema will be created by
  application code and Alembic migrations, so the same mechanism works in production (Phase 1).
- **Compose profiles are reserved for optional services** (for example a Redpanda web console or a
  containerized LLM), so the default stack stays minimal.

## Alternatives considered

- **Use Atlas for development too.** That needs a network connection, shares the 512 MB free quota
  with production, and can't be reset freely; tests would touch remote state.
- **A local Kubernetes (kind, or k3s in Docker) for development.** Closer to production, but heavier
  and slower to iterate with. Production parity comes in Phase 7 regardless.
- **Apache Kafka instead of Redpanda.** Kafka runs on the JVM and is heavier to run and configure.
  Redpanda is a single binary that speaks the same Kafka protocol, so client code is unchanged.
- **Redis instead of Valkey.** The protocol and clients are the same. Valkey is the BSD-licensed,
  community-governed fork.

## Consequences

- The idle stack fits comfortably; the limits cap the worst case at 3.3 GB.
- Mongo and R2 differ between dev and prod, so the `ObjectStore` interface and a version check
  against Atlas guard that gap.
- Running the services on the host means the containerized setup is first exercised in Phase 7.
  Integration tests in CI can use the same Compose file earlier if needed.

## Tools in plain language

- **Docker Compose:** starts a set of containers from one YAML file, with their networking, volumes,
  and health checks.
- **Redpanda dev-container mode:** a Redpanda setting that relaxes production checks, so the broker
  runs happily on one core with little memory.
- **Health check:** a command Docker runs inside a container to decide whether the service inside
  is ready.
