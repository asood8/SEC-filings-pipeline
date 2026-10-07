# Project Brief: SEC Filings Intelligence Pipeline

_Written October 2026. The free-tier facts in §9 were checked in October 2026 and change often, so re-verify any number before relying on it._

## 1. What it is

A system that:

1. Watches SEC EDGAR for new filings from a configurable watchlist of public companies.
2. Stores each raw filing and parses it by form type. Form 4 (insider trades) is structured XML; 8-K (major events) is free text organized into numbered items.
3. Summarizes free-text filings into short, structured summaries using a small LLM.
4. Makes everything searchable, by keyword and by meaning (semantic search).
5. Alerts signed-in users who follow a company, via Discord/Slack webhooks or email.

It runs on a schedule, so it generates its own activity. The data stays fresh and the free-tier services stay awake even with zero users.

## 2. Goals and non-goals

**Goals**
- A portfolio piece that is complex in **breadth**: many components companies actually use, each with a clear job, plus production practices (CI/CD, IaC, observability, tests, ADRs).
- Runs indefinitely at **$0/month** with no users. The only accepted cost is a domain (~$10/year) for a stable HTTPS address through Cloudflare Tunnel.
- Every component is defensible in an interview: "why X here?" has a concrete answer in an ADR.

**Non-goals**
- Investment advice or trading signals.
- High scale or high availability (a single-node cluster is fine).
- Real-time latency (minutes from filing to alert is fine).
- Every form type (start with Form 4 and 8-K).

## 3. Architecture

Pipeline: `discovery → filings.discovered → parser → filings.parsed → summarizer → filings.ready → notifier`

**Data flow**
1. A Kubernetes CronJob runs **discovery**. It polls the EDGAR submissions API for each watchlist company through the shared EDGAR client (User-Agent + Redis token bucket).
2. For each new filing, discovery saves the raw documents to **Cloudflare R2**. It then writes the filing row and an outbox event in **one Postgres transaction**.
3. An **outbox relay** publishes the event to **Redpanda** topic `filings.discovered`.
4. **parser** consumes it and routes by form type. The full parsed payload goes to **MongoDB Atlas**; key fields go to Postgres. It then publishes `filings.parsed`.
5. **summarizer** consumes `filings.parsed`:
   - Form 4 gets a deterministic templated summary, with no LLM.
   - 8-K gets the relevant items extracted, then an LLM summary (primary: local model; fallback: Gemini free tier).
   - It computes an embedding into **pgvector**, stores the summary in Postgres, and publishes `filings.ready`.
6. **notifier** consumes `filings.ready`, matches subscriptions, and sends Discord/Slack/email alerts with retries, a dead-letter queue, and dedupe.
7. Users visit the **React** site (Vite + TypeScript, static files) on Cloudflare Pages. It calls the **FastAPI** API, which is exposed through **Cloudflare Tunnel**. The API serves the feed, search (Postgres full-text + pgvector), Google sign-in, and subscriptions, and uses Redis for caching and rate limiting.

## 4. Components and why each exists

| Component | Tool | Runs on | Why it's here |
|---|---|---|---|
| Scheduling | Kubernetes CronJobs | k3s | Discovery runs on a schedule; no separate scheduler service eating RAM |
| Event backbone | Redpanda (Kafka API) | k3s | Decouples stages that run at different speeds (the LLM is slow); multiple consumers; **replay** to re-summarize history when the model or prompt changes |
| Relational DB + search | Postgres + full-text search + pgvector | k3s (block volume) | Companies, filings, summaries, users, subscriptions, outbox; one DB covers keyword and semantic search |
| Document store | MongoDB Atlas M0 | Atlas free tier | Parsed payloads have different shapes per form type (Form 4 transaction tables vs 8-K items) |
| Object storage | Cloudflare R2 (S3 API) | Cloudflare free tier | Raw filings stay out of the DBs; enables reparsing without re-hitting SEC |
| Shared state | Redis (Valkey) | k3s | Global SEC rate limit across all workers (token bucket), API rate limiting, caching, short-lived dedupe |
| LLM | llama.cpp server (or Ollama) with a small quantized model; Gemini free-tier fallback | k3s / Google | Free summarization; failover keeps the pipeline moving if one provider is down or throttled |
| API | FastAPI | k3s | Public read API, auth, subscriptions |
| Frontend | Vite + React + TypeScript (static) | Cloudflare Pages | Feed, search, company pages, subscription management |
| Ingress | Cloudflare Tunnel | k3s + Cloudflare | HTTPS access to the API without opening ports on the VM |
| Observability | OpenTelemetry SDK + collector → Grafana Cloud | k3s + Grafana free tier | Traces across services (including through Kafka headers), metrics, logs; offloaded to save VM RAM |
| CI | GitHub Actions | GitHub (public repo) | Lint, type-check, test; native arm64 image builds; push to GHCR |
| CD | Argo CD (GitOps) | k3s | Cluster state follows the manifests in git |
| IaC | Terraform (or OpenTofu) | n/a | Oracle VM/network, Cloudflare (tunnel, DNS, R2, Pages), Atlas, Grafana; makes the whole system rebuildable elsewhere |

## 5. Data sources (SEC EDGAR)

- **Fair access:** SEC documents a maximum of 10 requests/second and requires a declared User-Agent with contact info (https://www.sec.gov/os/accessing-edgar-data). We default to 5 req/s, enforced globally through Redis, because per-process throttles don't coordinate across workers.
- **Submissions API** (per company, JSON): `https://data.sec.gov/submissions/CIK##########.json`. The CIK is zero-padded to 10 digits.
- **Ticker ↔ CIK map:** `https://www.sec.gov/files/company_tickers_exchange.json`
- **Filing folder index:** `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-without-dashes}/index.json`
- **API docs:** https://www.sec.gov/edgar/sec-api-documentation
- **Scope to start:** a watchlist of ~25–50 large companies (config file), Form 4 and 8-K only. Poll on a configurable schedule (e.g., every 10–15 minutes on weekdays, hourly otherwise).

## 6. Data model (starting sketch; refine in Phase 1)

**Postgres**
- `companies(cik PK, ticker, name, exchange, on_watchlist)`
- `filings(accession_no PK, cik FK, form_type, filed_at, primary_doc_url, raw_object_key, status, created_at)`. Status moves through discovered → parsed → ready → notified, or failed.
- `summaries(id, accession_no FK, provider, model, prompt_version, summary_json, created_at)`. Keeps history; the latest row is current.
- `filing_embeddings(accession_no FK, model, embedding vector(N))`
- `users(id, email, google_sub, created_at)`
- `subscriptions(id, user_id FK, cik FK, form_types[], channel, target)`. Channel is discord | slack | email.
- `notifications(id, subscription_id FK, accession_no FK, status, attempts, last_error)` with UNIQUE(subscription_id, accession_no)
- `outbox(id, topic, key, payload_json, created_at, published_at)`

**MongoDB** `parsed_filings`: `{ accession_no, cik, form_type, parser_version, parsed: { ...form-specific... } }`

**Events:** JSON defined as Pydantic models with a `schema_version` field; message key = `accession_no`.
- Topics: `filings.discovered`, `filings.parsed`, `filings.ready`, plus a `<topic>.dlq` for each.
- Events carry IDs and pointers, not full documents.
- Propagate OpenTelemetry trace context in message headers.

## 7. Key design details

- **Idempotency:** unique constraints on `accession_no`; consumers upsert; the notifier enforces one alert per (subscription, filing), even across replays.
- **Transactional outbox:** discovery writes the filing row and its outbox event in one transaction, and the relay publishes afterward. There are no lost or phantom events.
- **Summarizer:**
  - Interfaces: `Summarizer` → `OpenAICompatibleProvider` (Ollama locally, llama.cpp server in prod), `GeminiProvider`, and `FailoverSummarizer` (circuit breaker, backoff on 429).
  - Extract the relevant 8-K items before prompting to keep prompts short.
  - Use JSON-schema-constrained output where the provider supports it, and always validate with Pydantic.
  - Store provider, model, and `prompt_version` with every summary.
- **Replay:** a CLI/Job that re-publishes `filings.parsed` (or resets a consumer group) to re-summarize history with a new model or prompt.
- **Object storage:** an `ObjectStore` interface with a local-filesystem implementation for dev/tests and an S3 implementation (boto3) for R2.
- **Metrics to track:**
  - filings discovered / parsed / ready per hour
  - consumer lag
  - summarizer latency by provider, and failover count
  - SEC limiter wait time, and 429 counts
  - notification success rate
- **Metric labels:** keep cardinality low (never use accession number as a label).
- **Alerts:** no filings ingested in 24h on a weekday, DLQ not empty, consumer lag high.
- **Backups:** a nightly `pg_dump` CronJob to R2.
- **VM memory budget (estimates; measure and update):**

  | Component | Est. RAM |
  |---|---|
  | k3s | ~0.6 GB |
  | Redpanda | ~1 GB |
  | Postgres | ~1–1.5 GB |
  | Valkey | ~0.2 GB |
  | Services | ~1 GB |
  | LLM (3B, 4-bit) | ~2–3 GB |
  | Argo CD | ~0.5–0.8 GB |
  | OTel collector | ~0.2 GB |
  | **Target** | **≤ 9 GB of 12 GB** |

## 8. Build plan

Each phase ends with something runnable, tests passing, and docs updated. Check items off here as they're completed.

- [x] **Phase 0: Foundations.**
  - Monorepo scaffold (§10), uv workspace
  - Docker Compose (Postgres+pgvector, Redpanda, Valkey, MongoDB), Makefile, pre-commit
  - GitHub Actions (lint, type-check, test)
  - ADR template + ADR-0001 (architecture overview), README skeleton
  - _Done when:_ `make up` starts the stack and CI is green.
- [x] **Phase 1: Thin vertical slice.**
  - Ticker→CIK loader, watchlist config
  - Shared EDGAR client (User-Agent, Redis token bucket, retries), discovery job
  - ObjectStore (local + R2), Postgres schema + Alembic, outbox relay
  - Form 4 parser → Mongo + Postgres, `GET /filings`
  - _Done when:_ real Form 4s for the watchlist appear through the API, and re-running discovery creates no duplicates.
- [x] **Phase 2: Summarization.**
  - 8-K item extraction, `Summarizer` interface, local + Gemini providers
  - Failover/circuit breaker, prompt versioning, replay job
  - _Done when:_ 8-Ks get validated structured summaries, stopping the local model fails over to Gemini automatically, and replay re-summarizes history.
- [x] **Phase 3: Search + API.**
  - Postgres full-text search, embeddings into pgvector, hybrid search endpoint
  - Company endpoints, pagination, OpenAPI docs, Redis caching + rate limiting
  - _Done when:_ keyword and semantic search both return sensible, tested results.
- [ ] **Phase 4: Users + alerts.**
  - Google OIDC sign-in, subscriptions CRUD, notifier consumer
  - Discord/Slack webhooks + email, retries with backoff, DLQ, one-alert-per-subscription guarantee
  - _Done when:_ a test subscription receives exactly one alert per new filing, even after a replay.
- [x] **Phase 5: Frontend.**
  - React static site (Vite + TypeScript): latest filings, search, company pages, filing detail (summary + link to the original on sec.gov), subscription management
  - _Done when:_ it works end to end against the local API.
- [x] **Phase 6: Observability.**
  - OTel instrumentation across services including Kafka header propagation
  - Metrics from §7, Grafana Cloud dashboards and alerts
  - _Done when:_ one trace follows a filing from discovery to alert.
- [ ] **Phase 7: Production.**
  - Terraform (OCI VM + network + block volume, Cloudflare tunnel/DNS/R2/Pages, Atlas, Grafana), k3s via cloud-init
  - Manifests with requests/limits, encrypted secrets, Argo CD
  - Multi-arch image builds to GHCR, llama.cpp server on arm64
  - _Done when:_ `terraform apply` plus an Argo CD sync brings up a working system, and a push to `main` deploys automatically.
- [ ] **Phase 8: Hardening.**
  - k6 load test of the API
  - Local model benchmark (speed vs summary quality across 2–3 small models/quantizations on the A1 VM)
  - Backups, data retention, runbook
  - README with architecture diagram and real metrics, ADRs complete

## 9. Free-tier facts (checked October 2026; re-verify)

- **Oracle Cloud Always Free**
  - Ampere A1 allowance is 2 OCPU + 12 GB RAM total (halved in June 2026), plus 200 GB block storage.
  - Instances must be in the tenancy's home region, and "out of host capacity" errors are common.
  - Idle instances may be reclaimed if CPU (95th percentile), network, and memory are all under 20% for 7 days.
  - Stay on the free account (don't upgrade to pay-as-you-go).
  - Includes 3,000 free emails/month through Email Delivery.
- **MongoDB Atlas M0:** 512 MB, free forever; auto-pauses after 60 days with zero connections.
- **Grafana Cloud Free:** 10k active metric series, 50 GB logs, 50 GB traces, 14-day retention, no credit card.
- **GitHub (public repo):** standard Actions runners are free and unlimited; native arm64 Linux runners (`ubuntu-24.04-arm`) are free; GHCR for images.
- **Gemini API free tier**
  - Flash-class models with per-model requests/minute, tokens/minute, and requests/day caps. Limits have changed several times, so check AI Studio for current numbers.
  - Free-tier inputs/outputs may be used by Google, which is fine because SEC filings are public.
  - Handle 429s with backoff and let the queue absorb delays.
- **Cloudflare free plan:** R2 (free tier, no egress fees), Tunnel, Pages. A stable Tunnel hostname needs a domain.
- **Avoid as the backbone:**
  - AWS: new accounts' Free plan ends after 6 months or when credits run out.
  - Supabase free: pauses after 1 week idle.
  - Upstash free: 500K commands/month, and blocking queue clients burn through it.

## 10. Repo layout (proposed)

```
services/
  discovery/  parser/  summarizer/  notifier/  api/
libs/common/        # config, logging, OTel setup, event models, EDGAR client, ObjectStore, Kafka helpers
web/                # React frontend (Vite + TypeScript)
deploy/k8s/         # manifests (Kustomize or Helm) + Argo CD apps
infra/terraform/    # modules: oci, cloudflare, atlas, grafana
docs/adr/           # architecture decision records
docs/PROJECT_BRIEF.md
tests/fixtures/     # saved EDGAR documents for parser tests
docker-compose.yml
Makefile
.github/workflows/
CLAUDE.md
```

## 11. Fallback plan

- **If Oracle A1 capacity isn't available:** keep retrying (try other availability domains), or pivot to one of:
  - a serverless variant: Cloud Run workers, Pub/Sub instead of Redpanda, Neon Postgres, Atlas, Gemini as the only LLM provider
  - the same k3s stack on an always-on spare machine at home behind Cloudflare Tunnel
- The summarizer already supports Gemini, and storage/LLM sit behind interfaces, so a pivot shouldn't touch business logic.

## 12. Open decisions (ask before choosing)

- ~~Watchlist: which companies and how many to start with~~ Decided 2026-10-05: 35 large, well-known US companies across sectors (`config/watchlist.toml`)
- ~~Kustomize vs Helm; Sealed Secrets vs SOPS for production secrets~~ Decided 2026-10-07: Kustomize and Sealed Secrets
- ~~Local observability viewer~~ Decided 2026-10-07: an optional grafana/otel-lgtm container (Compose profile); production uses Grafana Cloud
- Which local LLM and embedding model (pick a reasonable default in Phase 2; decide properly with the Phase 8 benchmark)
- ~~Next.js vs a lighter frontend~~ Decided 2026-10-06: Vite + React + TypeScript ([ADR-0015](adr/0015-frontend-stack.md))
- Project name and domain
- Later form types (10-Q/10-K section changes, 13F)
