# ADR-0017: Kubernetes manifests for the production cluster

- **Status:** Accepted
- **Date:** 2026-10-07

## Context

Production is a single-node k3s cluster on one Oracle Cloud Always Free VM (Ampere A1: **arm64,
2 OCPU, 12 GB**). The brief (§7) budgets at most **9 GB** of the 12 for everything, k3s and
Argo CD included, and asks for CPU and memory requests and limits on every workload. High
availability is a non-goal (§2).

- **What it has to run.** The five service images (ADR-0014) and the stores the cluster hosts
  itself: Postgres with pgvector, Redpanda, Valkey, and a local LLM. MongoDB (Atlas M0), object
  storage (Cloudflare R2), the fallback LLM (Gemini), and observability (Grafana Cloud) are
  managed services outside the VM.
- **No cloud accounts exist yet.** The VM, the tunnel, the buckets, and Atlas are created in
  Phase 7 with Terraform (ADR-0018), whose first-boot bootstrap installs k3s and Argo CD. The
  manifests must therefore render and validate without a cluster, in CI, and the first real apply
  comes later.
- **The repository is public,** so secrets can only be committed encrypted (brief §12).
- **Deployment is GitOps with Argo CD** (brief §4): the cluster follows `main`.
- **Two choices were the user's,** made on 2026-10-07: **Kustomize** rather than Helm, and
  **Sealed Secrets** rather than SOPS.

## Decision

### Layout

| Path | What it is |
|---|---|
| `deploy/k8s/base/` | The whole application, environment-neutral, in one namespace, `filings` |
| `deploy/k8s/overlays/production/` | Image registry and tags, replica counts, production settings, and (Phase 7) the SealedSecrets |
| `deploy/k8s/platform/sealed-secrets/` | The Sealed Secrets controller (`kube-system`), vendored upstream release |
| `deploy/k8s/secrets-example/` | Placeholder Secrets: the list of every secret, never deployed |
| `deploy/argocd/install/` | Argo CD itself, pinned upstream release, trimmed |
| `deploy/argocd/` | The Argo CD project and two Applications (the app and the Sealed Secrets controller) |

`deploy/k8s/README.md` is the operator's guide: rendering, the secrets and how to seal them, the
first install, and upgrades.

### What runs where

| Workload | Kind | Image | Copies | Notes |
|---|---|---|---|---|
| postgres | StatefulSet + 10 Gi volume | `pgvector/pgvector:0.8.7-pg18` (as in Compose) | 1 | Headless Service; tuned small (`shared_buffers=192MB`) |
| redpanda | StatefulSet + 10 Gi volume | `redpandadata/redpanda:v26.2.3` | 1 | `--smp=1 --memory=1G --overprovisioned`; topics created by the relay |
| valkey | Deployment, no volume | `valkey/valkey:9.1.2-alpine` | 1 | Everything in it is disposable; one store keeps the rate limits global |
| llm | Deployment + 8 Gi volume | `ghcr.io/ggml-org/llama.cpp:server-b11434` | 1 | Llama 3.2 3B Instruct Q4_K_M; OpenAI-compatible API on `http://llm:8080/v1` |
| embeddings | Deployment + 1 Gi volume | same llama.cpp image | 1 | nomic-embed-text v1.5 (16-bit), `--embeddings` |
| api | Deployment + Service | `filings-api` | 1 (overlay) | Ready on `GET /readyz` (Postgres answers), alive on `GET /healthz` |
| outbox-relay | Deployment | `filings-discovery`, `filings-outbox-relay` | exactly 1 | A second relay could publish out of order; heartbeat probe |
| parser | Deployment | `filings-parser` | 1 (overlay) | Up to 3 could share the 3 partitions; heartbeat probe |
| summarizer | Deployment | `filings-summarizer` | exactly 1 | The LLM serves one request at a time; a second copy would only queue; heartbeat probe |
| notifier | Deployment | `filings-notifier` | 1 (overlay) | More would be safe: the one-alert guarantee is a database row; heartbeat probe |
| discovery | CronJob, every 10 min | `filings-discovery` | 1 at a time | `concurrencyPolicy: Forbid`, no retries (the next run is 10 minutes away) |
| load-companies | CronJob, daily | `filings-discovery load-companies` | 1 | Run once by hand after the first install |
| migrate | Job, Argo CD hook | `filings-discovery`, `run_migrations()` | 1 | Before the services on every sync (below) |
| postgres-backup | CronJob, nightly | Postgres image (`pg_dump`) + `rclone/rclone:1.75.1` | 1 | Custom-format dump to the R2 backups bucket |
| cloudflared | Deployment | `cloudflare/cloudflared:2026.10.0` | 1 (overlay) | Cloudflare Tunnel to the api Service; no ingress controller |
| otel-collector | Deployment + Service | `otel/opentelemetry-collector-contrib:0.162.0` | 1 | OTLP/HTTP in, Grafana Cloud out |
| sealed-secrets-controller | Deployment (`kube-system`) | `bitnami/sealed-secrets-controller:0.40.0` | 1 | Decrypts SealedSecrets |
| Argo CD | `argocd` namespace | `quay.io/argoproj/argocd:v3.5.4`, Redis | 1 each | Dex, notifications, and ApplicationSet scaled to 0 |

Every image publishes `linux/arm64` (checked for each tag), and every tag is pinned to a version.

- **Single replicas are deliberate,** for ordering (the relay), for memory (the LLM servers,
  the summarizer that feeds them), or because one is plenty at about 300 filings a month. Where
  more copies would be safe (api, parser, notifier, cloudflared), the production overlay sets the
  count. Workers that must never run twice use the `Recreate` strategy, so an update stops the old
  pod before starting the new one.
- **Discovery is a CronJob,** not a service: nothing runs between polls. Its settings: every
  10 minutes in UTC, a slow run is never overlapped and a missed one is skipped
  (`startingDeadlineSeconds: 300`), and each run has 9 minutes (`activeDeadlineSeconds: 540`).
- **The k3s bundled Traefik ingress controller and service load balancer are disabled at install**
  (the k3s configuration in the VM's cloud-init, ADR-0018), saving their memory. Cloudflare
  Tunnel makes an outbound connection to Cloudflare and forwards the public hostname to the `api`
  Service, so the VM opens no ports and needs no ingress.

### The local LLM and embeddings

- **llama.cpp's server** (official image, build `b11434`) serves the same OpenAI-compatible API the
  summarizer already uses with Ollama, so production only changes `LLM_BASE_URL`, `LLM_MODEL`, and
  `LLM_PROVIDER_NAME=llama.cpp`. It is a single binary with no model manager, and it exposes the
  memory knobs used below.
- **The model:** Llama 3.2 3B Instruct, Q4_K_M, from `bartowski/Llama-3.2-3B-Instruct-GGUF`
  (ungated, Llama 3.2 license): the same model and quantization type as Ollama's `llama3.2:3b`,
  the development default (ADR-0010), though not the same file, since bartowski quantizes with an
  importance matrix. The two files are the same size, so they need the same memory. The embedding
  model is `nomic-ai/nomic-embed-text-v1.5-GGUF`, 16-bit, the same weights as Ollama's
  `nomic-embed-text:v1.5`.
- **An init container downloads each model into its volume once,** from a URL pinned to a Hugging
  Face commit, and checks its SHA-256 (`base/fetch-model.sh`). An existing file is re-checked on
  every start, so a corrupt or half-written download is fetched again instead of served. A wrong
  checksum fails the pod and leaves nothing behind.
- **Settings for the 2-core, memory-capped VM:** an 8,192-token context (prompts reach about 6k),
  one request at a time, 2 threads, an 8-bit KV cache with flash attention, and **direct I/O**
  model loading (`--load-mode dio`), so the weights sit in the process's own memory rather than in
  page cache charged unpredictably to the container.
- **Embeddings run as a second llama.cpp server,** with its own volume and limits, so either can
  restart without the other. It fits the budget (measured below), so semantic search works in
  production.

### Resource budget

Requests are what the scheduler reserves (and, for CPU, how busy cores are shared); limits are
the most a container may use. Memory is budgeted at the **limits**, so even if everything peaks
at once the node isn't overcommitted. CPU limits only cap bursts, so they may add up to more than
the 2 cores.

| Component | CPU request / limit | Memory request / limit | Basis (measured unless noted) |
|---|---|---|---|
| llm | 500m / 2 | 2.5 Gi / 3 Gi | 2.53 GiB peak, largest 8-K |
| embeddings | 50m / 1 | 384 Mi / 512 Mi | 396 MiB peak |
| redpanda | 200m / 1 | 512 Mi / 1.25 Gi | Set: `--memory=1G` (its minimum per core) plus overhead; 88 MiB idle |
| postgres | 100m / 1 | 384 Mi / 768 Mi | 146 MiB after initialization |
| valkey | 20m / 250m | 32 Mi / 192 Mi | Set: `maxmemory 128mb` |
| api | 200m / 1 | 128 Mi / 256 Mi | 98 MiB (dev, telemetry on) |
| parser, summarizer | 20m / 500m each | 128 Mi / 256 Mi each | 92–93 MiB each (dev, telemetry on) |
| outbox-relay, notifier | 20m / 250m each | 96 Mi / 144 Mi each | 73–76 MiB each (dev, telemetry on) |
| otel-collector | 50m / 500m | 64 Mi / 256 Mi | 45 MiB warm, 143 MiB on a cold start (below) |
| cloudflared | 20m / 250m | 32 Mi / 128 Mi | Not measured (needs a real tunnel) |
| sealed-secrets-controller | 20m / 200m | 32 Mi / 128 Mi | Not measured |
| **Long-running pods** | **1.24 cores** | **4.47 Gi / 7.22 Gi** | |
| Jobs, two at once at most (discovery + a daily job) | 100m | 0.41 Gi (limits) | 77 MiB after imports |
| Argo CD (controller, repo server, API server, Redis) | 180m | 0.75 Gi (limits) | Not measured |
| k3s itself and its add-ons (brief §7 estimate) | 200m | ~0.6 Gi | |
| **Total** | **1.72 of 2 cores** | **8.97 GiB of the 9 GiB budget** | |

`tests/test_k8s_manifests.py` recomputes these totals from the rendered manifests and fails if
they exceed the budget, so adding a workload means trimming another or revisiting the budget.

**How the numbers were measured.** On the dev laptop (amd64), with each container run as the
manifests run it (same UID, read-only root filesystem, no capabilities, Docker memory and CPU
caps), reading the kernel's `memory.peak` for the container:

- **llm:** 2.53 GiB peak while summarizing an 8-K with a long press release (Adobe's earnings
  release padded to the per-exhibit cap: the summarizer's real request, 3,787 prompt tokens) with
  2 CPUs: 293 s in total, prompts at 14 tokens/s and generation at 4.6 tokens/s. The KV cache is
  allocated for the full 8,192-token context at startup, so longer prompts take longer but don't
  need more memory. Measured with Ollama's `llama3.2:3b` file, which has the same model,
  quantization type, and size as the one the manifest downloads. Without direct I/O, page cache
  pushed the same run to the 3.5 GiB cap. A 16-bit KV cache needed 0.4 GiB more than the 8-bit
  one at about the same speed (prompts 7% faster, generation 7% slower).
- **embeddings:** 396 MiB peak embedding 8 documents of about 480 words with the project's own
  client (768 dimensions, normalized), 1 thread, 22.7 s.
- **Both servers** started with the exact arguments from the rendered manifests and answered
  `/health`, `/v1/models`, and a chat completion. The download script, under the init container's
  256 MiB limit, fetched and verified a file larger than that limit, rejected a wrong checksum
  without leaving a file behind, and skipped a file already present.
- **Postgres and Valkey** started from fresh, world-writable data directories (as local-path
  provides) as their image users (999, and 999:1000) with read-only roots; `CREATE EXTENSION
  vector` worked (0.8.7).
- **Redpanda** started healthy with the manifest's node and bootstrap configuration, created a
  topic, and answered its readiness endpoint. On a small test volume it first reported itself
  unhealthy: by default it wants 5 GiB free and refuses writes below that, so the bootstrap
  configuration lowers the threshold to 1 GiB.
- **otel-collector:** the contrib binary is about 400 MB, and on a cold start (after a reboot,
  with none of it in page cache) the pages the collector reads count against its own limit. Run
  from a copy of the binary written past the page cache, at 0.5 CPU, it was healthy after 5 s
  with a 256 MiB limit (143 MiB used), but needed 21 s at 160 MiB, and 44 s at 160 MiB and
  0.25 CPU, reclaiming and re-reading its own code as it went. One start at 160 MiB and 0.25 CPU,
  right after the image was pulled on a laptop short of memory, never became healthy in
  4 minutes. Warm, it settles at about 45 MiB. Hence the 256 MiB limit, the 500m CPU limit, and a
  startup probe. The configuration passed the image's own `validate` command, and the collector
  accepted an OTLP/HTTP export.
- **Python services:** resident memory of each stage running locally with tracing and metrics on,
  and 77 MiB for the discovery image after importing everything a job needs.

These are laptop numbers, and the A1 is likely slower per core. `LLM_TIMEOUT_SECONDS` is 600,
about twice the measured run. Handling one message, retries included, must fit in the summarizer's
Kafka poll interval, or the broker hands the message to a new consumer and it is never
dead-lettered. The summarizer therefore derives that interval from its settings (every attempt
waiting out each provider's timeout, plus the backoff: about 72 minutes with these values), so a
filing both providers fail on is retried and dead-lettered as designed, with or without a Gemini
key. The longest prompts (about 6k tokens, with several exhibits) may exceed the timeout on the
VM; those fall back to Gemini. The Phase 8 benchmark (`filings-benchmark`) measures this on the
VM.

### Probes and shutdown (added 2026-10-07)

- **The workers prove they aren't hung with a heartbeat.** The relay, parser, summarizer, and
  notifier serve no HTTP, so Kubernetes used to restart them only when the process exited. Now each
  one's main loop keeps a heartbeat file in `/tmp` (the pod's `emptyDir`; the root filesystem is
  read-only), and an exec probe runs `filings-healthcheck <process name>`, which fails once the
  heartbeat is overdue (`filings_common.health`).
- **The file holds a deadline, not the time of the last beat**: when the loop promises to beat
  again. The loops beat on every pass (about once a second when idle) and promise the next beat
  within a 2-minute window, generous because the slowest ordinary step (publishing while the broker
  is unreachable, or measuring consumer lag) takes about 40 s. Before each attempt at a message, a
  consumer promises that attempt's own limit instead: for the summarizer every configured model
  timeout plus a minute (13 minutes with the production settings), for the parser and notifier
  their Kafka poll interval (5 minutes), past which the broker has given the partition away anyway.
  So a summarizer working through a long 8-K is never killed mid-summary, yet an attempt that
  outlives every timeout it could be waiting on is caught, and so is a loop that hangs anywhere
  else, within a window. The file is rewritten only when the deadline moves by half a window, about
  once a minute.
- **Probe settings:** a startup probe allows 5 minutes for the first beat (every 10 s, 30 failures),
  then the liveness probe runs every 30 s and restarts the container after 2 failures, so a hung
  idle loop is restarted within about 4 minutes. Each check is a fresh Python process that imports
  nothing heavy: about 40 ms of CPU and 16 MiB of memory at its peak (measured on the laptop), well
  inside each worker's limit (the smallest is 144 MiB; the workers peak at 75-90 MB). Its timeout
  is 5 s rather than the default 1 s, for a node whose cores the LLM keeps busy. A new process
  removes the file a previous container in the same pod left behind, so the probes wait for its
  own first beat.
- **The API is ready only while Postgres answers.** `GET /readyz` runs `SELECT 1` and returns 503
  when that fails or takes over 2 s. It is the readiness probe (every 10 s, 3 failures, 5 s
  timeout), so a replica without its database gets no traffic and a rollout (`maxUnavailable: 0`)
  waits until the new pod can serve. `GET /healthz` stays the liveness probe and checks nothing
  else: a database outage is no reason to restart the API. Like `/healthz`, `/readyz` is never
  rate limited, cached, or traced. It is reachable through the tunnel, so it says only `ok` or
  `unavailable` (the log says why, once per change), and nobody can turn it into a flood of
  database work: one check runs at a time, concurrent requests share its answer, an answer is
  reused for a second, and a check that times out is abandoned in its worker thread, with no new
  check reaching the database until that thread returns. A hung database therefore ties up one
  thread, not one per probe (`filings_api.readiness`).
- **Grace period: 45 s for every pod that exports telemetry** (the default is 30 s): the five
  service Deployments and the discovery and load-companies CronJobs. After SIGTERM a service
  finishes its current step (the API its requests in flight), then flushes its last spans and
  metrics, which gives up after 30 s with the collector unreachable (ADR-0016). The summarizer
  doesn't wait for a summary in progress, which can take minutes; it is killed after the grace
  period, and the filing is redelivered and summarized again.
- **Discovery now stops on SIGTERM.** As PID 1 without a handler, Python ignored SIGTERM
  (ADR-0014), so a run that outlived its `activeDeadlineSeconds` was killed at the end of the grace
  period and its buffered spans were lost, though that run's trace is the one worth reading. It
  now exits at once through its `finally` blocks: the filing in progress is rolled back, as when
  killed, and the spans so far are exported. The migrate Job keeps the default grace: it exports
  no telemetry, and still ignores SIGTERM.

### Sync order

Argo CD applies resources in **sync waves**, lowest first, and waits until each wave is healthy:

| Wave | Resources |
|---|---|
| -4 | The `filings` namespace |
| -3 | The config ConfigMap and the SealedSecrets (so each Secret exists before any pod reads it) |
| -2 | Postgres |
| -1 | The migrations Job |
| 0 | Everything else |

**Migrations run as a Sync hook in wave -1, not a PreSync hook.** A PreSync hook runs before
anything else in the sync, Postgres included, so on an empty cluster the very first sync could
never succeed. As a Sync hook in wave -1 it runs on every sync, after Postgres is healthy and
before any service rolls out, so new code never meets an old schema; an up-to-date database makes
it a no-op. `BeforeHookCreation` keeps the last run and its logs until the next sync.

### Configuration and secrets

- **Settings** are environment variables with the exact names from the settings classes. The
  non-secret ones are in `base/config.env`, and the production overlay merges its values over
  them. Kustomize turns them into one ConfigMap whose name includes a hash of its content and
  rewrites every reference, so changing a setting rolls out the pods that use it. A test checks
  that every key is a real setting name.
- **`DATABASE_URL`** is assembled in each workload from `POSTGRES_DB` and the `postgres` Secret's
  user and password (`$(VAR)` references in `env`), so the password has one source. It goes into a
  URL, so it must be URL-safe (hex).
- **Secrets are committed only as SealedSecrets** in the production overlay, made with `kubeseal`
  and the cluster's public certificate (Phase 7). `deploy/k8s/README.md` lists every secret with
  its keys and consumers, and documents the workflow. In short:

  | Secret | Contents |
  |---|---|
  | `postgres` | Database user and password |
  | `mongodb` | Atlas connection string |
  | `r2-raw-filings` | R2 endpoint and a token for the raw-filings bucket |
  | `r2-backups` | R2 endpoint and a separate token for the backups bucket only |
  | `sec-edgar` | SEC contact email (personal, so not in public config) |
  | `gemini` (optional) | Gemini API key |
  | `smtp` (optional) | SMTP user and password, once `EMAIL_BACKEND=smtp` |
  | `cloudflared` | Tunnel token |
  | `grafana-cloud` | OTLP endpoint, instance ID, and access token |

  `GOOGLE_CLIENT_ID` is public (the website embeds it), so it is config, as are bucket names and
  the SMTP host and sender.
- **Placeholder Secrets** in `deploy/k8s/secrets-example/`, each headed "EXAMPLE ONLY", are the
  templates to fill in. Filled-in copies go in `deploy/k8s/secrets-local/`, which is gitignored
  (as are `*.unsealed.yaml` and the sealing-key backup). A test fails if the production render
  ever contains a plain Secret, and another checks that every Secret a workload reads has an
  example with its keys.
- **The Sealed Secrets controller is vendored,** not fetched: the upstream release's
  `controller.yaml` (11 KB) is committed byte-for-byte, so it is reviewed in git and renders
  offline, and `make k8s-check` verifies its SHA-256 against the release. Argo CD's install
  manifest (1.9 MB) is instead a Kustomize remote resource pinned to its release tag; vendoring it
  would bury the repository's own changes in diffs nobody reads.

### Argo CD

- **Two Applications** in one AppProject: `filings` (the production overlay) and `sealed-secrets`
  (the controller). Both track `main` with **automated sync, prune, and self-heal**: what is
  removed from git is deleted from the cluster, and changes made by hand are undone. `PruneLast`
  deletes leftovers (old hashed ConfigMaps) only after everything else is healthy.
- **A new cluster gets going without help.** The VM's bootstrap (ADR-0018) applies Argo CD and
  both Applications at once. Sealing a secret needs the controller's certificate, so on a first
  install the Secrets can only be committed after that, and until then the first sync waits on a
  Postgres that can't start. By default an Argo CD sync waits for health forever and blocks every
  later commit, so the install sets a **30-minute sync timeout**, and failed syncs **retry from
  the latest commit** (`retry.refresh`), with backoff. Once the SealedSecrets are pushed, the next
  attempt applies them. The same retries cover a rebuilt cluster, whose SealedSecrets are already
  in git but whose SealedSecret kind doesn't exist until the controller is up.
- **The AppProject** allows only this repository, only the `filings` and `kube-system`
  namespaces, and only the cluster-wide kinds the manifests contain.
- **No resources finalizer:** deleting an Application leaves the system and its data running.
- **Argo CD itself** is the upstream non-HA install, pinned to v3.5.4, with the components this
  cluster doesn't use scaled to zero and requests and limits on the rest. Its web UI isn't exposed;
  `kubectl port-forward` reaches it. The bootstrap installs it and the two Applications; from
  then on Argo CD manages both from git.

### Security posture

- **Pod Security Admission enforces the "restricted" profile** on the namespace, so the API server
  itself rejects a pod that could run as root, escalate privileges, keep Linux capabilities, or skip
  the default seccomp profile.
- **Every container** runs as a non-root numeric UID (10001 for our images and llama.cpp, whose
  image defaults to root; each third-party image's own user otherwise), with all capabilities
  dropped, no privilege escalation, the `RuntimeDefault` seccomp profile, and a **read-only root
  filesystem** (an `emptyDir` for `/tmp` and other scratch paths). No pod gets a Kubernetes API
  token or Service environment variables, which could also collide with setting names such as
  `LLM_PORT`.
- **NetworkPolicies** (k3s enforces them with its embedded controller): all inbound traffic is
  denied by default, then opened per port to the pods that need it:

  | Destination | Allowed from |
  |---|---|
  | postgres :5432 | the services, discovery, load-companies, migrate, postgres-backup |
  | redpanda :9092 | outbox-relay, parser, summarizer, notifier |
  | valkey :6379 | api, discovery, load-companies, summarizer |
  | llm :8080 | summarizer |
  | embeddings :8080 | summarizer, api |
  | api :8000 | cloudflared only |
  | otel-collector :4318 | any pod in the namespace |

  The data stores may only reach DNS and pods in the namespace, so a compromised store can't send
  data out. The API admits only cloudflared, which is what makes `API_TRUST_PROXY_HEADERS=true`
  safe: no other client can reach it to forge `CF-Connecting-IP`.
- **Nothing is exposed directly:** no ingress controller, no NodePorts, no LoadBalancers. The
  tunnel is the only way in, and the Argo CD UI needs `kubectl` access.
- **Supply chain:** every image and upstream manifest is pinned to a version; the models are
  pinned to a commit and a checksum; the validation tools are checksum-verified downloads.

### Backups and observability

- **Nightly backup** (03:30 UTC): an init container runs `pg_dump --format=custom` with the
  Postgres image itself, so the client always matches the server's major version, then rclone
  uploads the file to the R2 backups bucket with a token that can touch only that bucket. Retention
  is a lifecycle rule on the bucket (Terraform), so the job never deletes anything.
- **Telemetry:** the services send traces and metrics over OTLP/HTTP to the in-cluster collector
  (`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`, the contract from ADR-0016). The
  collector batches them, refuses data rather than run out of memory (`memory_limiter`), and
  forwards them to Grafana Cloud with basic auth from the `grafana-cloud` Secret. No observability
  backend runs on the VM.

### Validation without a cluster

`make k8s-tools` installs pinned, checksum-verified `kubectl`, `kubeconform`, and `kubeseal` into
`~/.local/bin`. `make k8s-check`, also a CI job on every push to `main` and every pull request:

1. verifies the vendored Sealed Secrets manifest's checksum;
2. renders every Kustomize directory with `kubectl kustomize`;
3. validates every object with **kubeconform**, strictly (unknown fields are errors), against the
   Kubernetes 1.36 schemas (k3s's stable channel). SealedSecret, Application, and AppProject are
   custom resources; their schemas come from the CRDs-catalog repository. Both schema sources are
   pinned to a commit. The CustomResourceDefinitions themselves (upstream ones only) get the
   non-strict schema, since there is no strict one for them;
4. runs `tests/test_k8s_manifests.py`: requests and limits everywhere, the budget, the restricted
   profile, probes on every long-running container (each worker's on the heartbeat its process
   writes), the grace periods, pinned images, real setting names, an example for every Secret, and
   no plain Secrets. `make test` runs these too when kubectl is installed.

It needs the internet (schemas, cached; Argo CD's manifest), but no cluster and no cloud account.

**Checked once against a real API server.** A throwaway k3s v1.36.5 server ran in Docker on the
laptop, without an agent, so nothing could actually run. With the CRDs installed, every object in
the four renders passed a server-side dry run. Every pod template, submitted as a Pod, was admitted
under the namespace's restricted profile, and a control Pod without those settings was refused.

## Alternatives considered

- **Helm** (the user chose Kustomize). Helm templates YAML with Go templates and values files,
  and packages charts for others to install. This repository deploys one application to one
  environment, where templates add a layer of indirection between what is written and what is
  applied. Kustomize patches plain YAML, is built into `kubectl` and Argo CD, and needs no extra
  tool. Upstream components (Argo CD, Sealed Secrets) also ship plain manifests, so Helm isn't
  needed for them either.
- **SOPS** (the user chose Sealed Secrets). SOPS encrypts values inside files with keys we manage
  (age or a cloud KMS), and Argo CD needs a plugin to decrypt them. Sealed Secrets needs only a
  small controller and `kubeseal`; the private key never leaves the cluster, and anyone with the
  public certificate can seal a secret. The cost is that SealedSecrets are bound to one cluster's
  key, which must be backed up to survive a rebuild.
- **Migrations as a PreSync hook** (the task's first suggestion). Explained above: it would
  deadlock the first sync on an empty cluster.
- **Migrations in an init container of each service.** Every replica and every restart would race
  to migrate, and every service would need schema-changing rights.
- **Ollama in production,** as in development. It runs llama.cpp underneath, plus a model manager
  this cluster doesn't need, and hides the KV-cache and loading options used above.
- **One llama.cpp server for both models.** One process would mean one memory limit and one
  restart for both.
- **Operators** (CloudNativePG for Postgres, the Redpanda operator). They bring failover, backups,
  and upgrades, which a single replica doesn't need, at the cost of another controller's RAM and
  CRDs. Plain StatefulSets are about 130–180 lines each.
- **A Kustomize remote resource for Sealed Secrets.** It would render from the network and keep
  the manifest out of review; at 11 KB, vendoring is cheap.
- **Validating against a throwaway cluster in CI** (kind or k3d, then `kubectl apply
  --dry-run=server`). It catches more, such as admission errors, but adds minutes, gigabytes of
  images, and flakiness to every run. kubeconform is offline and takes seconds; a real cluster
  check can come with Phase 7.

## Consequences

- **Not yet run on a cluster or on arm64.** The first apply is in Phase 7. The containers were
  started individually as the manifests run them, but these were not exercised: the
  NetworkPolicies under k3s's controller, local-path volume permissions on the VM, llama.cpp's
  speed on the A1, cloudflared with a real tunnel, the backup upload to R2, and the memory of
  Argo CD and the Sealed Secrets controller under their limits.
- **The budget is nearly full** (8.97 of 9 GiB at limits). Most of it is the LLM (3 GiB) and
  Redpanda (1.25 GiB). If more is needed: drop the embeddings server (semantic search falls back
  to keywords), shrink Redpanda, or put Gemini first and the local model on demand.
- **CPU is shared.** While the LLM summarizes an 8-K, it keeps both cores busy for minutes. The
  API's higher CPU request keeps it a fair share meanwhile; the Phase 8 load test on the VM will
  show whether that is enough. Fewer LLM threads would trade summary speed for API latency.
- **The model file may change in Phase 8.** llama.cpp can repack Q4_0 weights at load time for
  ARM's dot-product instructions, which the A1's Neoverse N1 cores have, so a Q4_0 file may
  process prompts much faster there than Q4_K_M, at some cost in quality. The Phase 8 benchmark
  should compare them on the VM. Switching is a change to the model URL, checksum, and
  `LLM_MODEL`.
- **The sealing key must be backed up** (`deploy/k8s/README.md`). Without it, a rebuilt cluster
  can't decrypt the SealedSecrets in git, and every secret must be sealed again.
- **Deploying a new version is a commit.** The production overlay pins image tags; Phase 7's
  image-build CI must update them (or Argo CD Image Updater must) for a push to `main` to deploy
  automatically.
- **Placeholders wait on the real accounts:** the website's origin (`API_CORS_ORIGINS`, a
  Terraform output), `GOOGLE_CLIENT_ID`, the SMTP settings, and the SealedSecrets. Until then the
  features they control stay off or harmless. The tunnel's routing rule and the backups bucket's
  lifecycle rule are Terraform's (ADR-0018).
- **What the service code could add:**
  - `filings-replay` and `filings-index` don't handle SIGTERM (ADR-0014), so a stopped run is
    killed after the grace period, without its final telemetry export. That is safe, as each
    filing is one transaction; they run by hand, not in the cluster. (`filings-discovery` stops on
    SIGTERM since 2026-10-07; see "Probes and shutdown".)
  - The summarizer finishes its current filing before stopping, but an 8-K can take minutes, so
    an update kills it mid-summary after the 45 s grace period. The filing is redelivered and
    summarized again, which is idempotent; only the work is lost.
  - Liveness and readiness (heartbeat probes, `/readyz`) and the longer grace periods were added
    on 2026-10-07; see "Probes and shutdown" above.
- **With one API replica, a database outage takes the API off the tunnel** (after 30 s of failed
  readiness checks): Cloudflare answers with its own error page instead of the API's 500s, and the
  response cache can't serve anything meanwhile. Every endpoint but `/healthz` needs Postgres, so
  little is lost; a second replica wouldn't help, since both would share the database.
- **Upgrades are manual and listed** in `deploy/k8s/README.md`. A dependency bot (Renovate) could
  propose them later.

## Tools in plain language

- **Kustomize:** builds Kubernetes YAML from a base plus overlays, without templates. The base is
  plain manifests; an overlay lists changes such as "use this image tag" or "run 2 replicas", and
  Kustomize merges them. Here the base is the whole app and the production overlay adds what is
  specific to the real cluster. `kubectl kustomize <dir>` prints the result.
- **Sealed Secrets:** a way to keep secrets in a public git repository. `kubeseal` encrypts a
  Secret with the cluster's public key into a SealedSecret, which is safe to commit; a controller
  in the cluster, the only holder of the private key, turns it back into a normal Secret.
- **Argo CD sync:** Argo CD watches the repository and makes the cluster match it. A **sync**
  applies the difference; **automated** sync does it on every new commit, **prune** deletes what
  was removed from git, and **self-heal** undoes changes made directly in the cluster. **Sync
  waves** order the steps, and a **hook** is a one-off Job run as part of a sync, here the
  database migrations.
- **CronJob:** a Kubernetes object that starts a Job on a schedule written like a Unix crontab
  (`*/10 * * * *` is every ten minutes). `concurrencyPolicy: Forbid` skips a run while the
  previous one is still going.
- **Probes:** checks the kubelet runs against each container. A **liveness** probe that keeps
  failing gets the container restarted; while a **readiness** probe fails, the pod gets no traffic
  from its Service; a **startup** probe holds the other two off until the container has started.
  A probe is an HTTP request, or (`exec`) a command run inside the container that passes by exiting
  with 0. **terminationGracePeriodSeconds** is how long a stopping pod has between SIGTERM and being
  killed.
- **NetworkPolicy:** a firewall rule between pods, by label. Once a pod is selected by a policy,
  only the traffic the policy allows can reach it; here a default policy blocks everything and
  each service opens just its port to its clients.
- **Pod Security Admission:** a check built into the Kubernetes API server. Labelling the
  namespace "restricted" makes it reject any pod that asks for more privileges than an ordinary
  unprivileged process.
- **StatefulSet:** like a Deployment, but each pod keeps a stable name and its own persistent
  volume across restarts, which databases need.
- **k3s local-path:** k3s's built-in storage, which gives each volume claim a directory on the
  node's disk (here the VM's block volume).
- **kubeconform:** checks rendered manifests against the official Kubernetes API schemas without
  a cluster, so typos and unknown fields fail in CI instead of at deploy time.
- **llama.cpp server and GGUF:** llama.cpp runs LLMs efficiently on CPUs; its server offers an
  OpenAI-compatible HTTP API. GGUF is its model file format, which holds quantized weights (here
  about 4.5 bits per weight for the LLM).
- **cloudflared (Cloudflare Tunnel):** a small agent that connects out to Cloudflare and carries
  requests for a public hostname back to a service inside the cluster, so nothing needs an open
  port.
- **OpenTelemetry Collector:** a relay for telemetry. Services send traces and metrics to it, and
  it batches and forwards them to a backend, here Grafana Cloud.
