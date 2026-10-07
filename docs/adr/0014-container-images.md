# ADR-0014: Container images for the services

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Phase 7 runs the five services on k3s on an Oracle Ampere A1 VM, which is **arm64** (2 OCPU,
12 GB). The dev laptop is **amd64**. Every image must build for both.

- The services live in one uv workspace with one `uv.lock` (ADR-0002). Each service depends on
  `filings-common`, which also provides three commands that run as their own processes:
  `filings-outbox-relay` (long-running), `filings-replay`, and `filings-index` (one-off jobs).
- Images should be small, built from exactly the locked dependency versions, and quick to rebuild
  when only code changes.
- No secret and no dev data may reach an image, because the repository and its images are public.
- Kubernetes should be able to check that containers don't run as root.
- This laptop has no QEMU emulation for arm64, and installing it changes host kernel settings, so
  arm64 images can't be built here yet. Building and pushing images in CI is Phase 7 work.

## Decision

### One parameterized Dockerfile

`docker/service.Dockerfile` builds every service image. `--build-arg PACKAGE=filings-<service>`
selects the workspace package to install and the image's default command. `make images` builds all
five for the local platform, tagged `filings-<service>:dev`. `make image-<service>` builds one.

```
docker build -f docker/service.Dockerfile --build-arg PACKAGE=filings-api -t filings-api:dev .
```

### Stages

1. **Builder.** Starts from `python:3.12.15-slim-trixie`, with the uv binary copied from
   `ghcr.io/astral-sh/uv:0.12.23` (the uv version used in development). It installs into a
   virtualenv at `/opt/venv` in two steps:
   1. **Third-party dependencies**, from only the lockfile and the `pyproject.toml` files:
      `uv sync --locked --no-dev --no-editable --no-install-workspace --package $PACKAGE`.
      This layer is rebuilt only when dependencies change.
   2. **The code**: `filings-common` and that one service, installed with the same command minus
      `--no-install-workspace`. A change to another service doesn't touch this image.
   - `--locked` fails the build if `uv.lock` is out of date.
   - `--no-editable` installs real copies of the packages, so the venv doesn't depend on the
     source tree.
   - A BuildKit cache mount keeps uv's download cache between builds, outside the image.
   - Bytecode is compiled at build time. The venv is read-only at runtime, so Python couldn't
     cache bytecode itself and would recompile every module it imports on each start.
2. **Runtime.** The same Python image plus `/opt/venv`, and nothing else from the builder: no uv,
   no source tree, no build tools.
   - It runs as UID/GID **10001**. The user is numeric, so Kubernetes can enforce
     `runAsNonRoot` without looking up a name.
   - Everything in the image is owned by root, so the process can't modify its own code. Anything
     it writes needs a volume.
   - It sets `PYTHONUNBUFFERED=1` so logs appear immediately, and `PYTHONDONTWRITEBYTECODE=1`.
   - The working directory is `/app`, so relative settings paths resolve there.
3. **One small final stage per package**, which sets the default command. The last line,
   `FROM cmd-${PACKAGE}`, picks that stage. A missing `PACKAGE` fails the build with a message, and
   an unknown one fails because no such stage exists.

| Image | Default command | Production role (Phase 7) |
|---|---|---|
| `filings-api` | `uvicorn --factory filings_api.app:create_app --host 0.0.0.0 --port 8000` | Deployment |
| `filings-discovery` | `filings-discovery run` | CronJob, plus the outbox relay and one-off jobs (below) |
| `filings-parser` | `filings-parser` | Deployment |
| `filings-summarizer` | `filings-summarizer` | Deployment |
| `filings-notifier` | `filings-notifier` | Deployment |

### Shared commands, migrations, and configuration

- **Every image includes the `filings-common` commands.** In production the **discovery image runs
  the outbox relay**, as a Deployment that overrides the command with `filings-outbox-relay`. It
  also runs the one-off jobs: `load-companies`, `fetch-exhibits`, `filings-replay`,
  `filings-index`, and migrations. Discovery's dependencies are exactly `filings-common`'s (the
  API image adds a web server), it is already the batch-job image, and having one image for
  pipeline operations means nobody has to guess which one to use.
- **Migrations** run from the discovery image with
  `python -c "from filings_common.db import run_migrations; run_migrations()"`, reading
  `DATABASE_URL`. The migration scripts ship inside the `filings_common` package, and
  `alembic_config()` sets their location in code, so the image needs no `alembic.ini`.
- **The discovery image includes `config/watchlist.toml`** at `/app/config/watchlist.toml`, the
  default `DISCOVERY_WATCHLIST_PATH`. It is public configuration, so the image works without extra
  setup. To change it without a rebuild, mount a file over it (a ConfigMap) or point the variable
  elsewhere.
- **Configuration comes from environment variables only.** There is no `.env` in any image. The
  settings classes still look for one in `/app` and find nothing, which is harmless. Production
  supplies variables from encrypted secrets and ConfigMaps (brief §12).

### What gets into an image

`.dockerignore` is an **allowlist**. The build context contains only the root `pyproject.toml` and
`uv.lock`, each package's `pyproject.toml` and `src/`, and `config/watchlist.toml`. `.env`,
`.env.*`, `.git`, `.venv`, `data/`, tests and fixtures, caches, and `node_modules` are also excluded
by name, in case the allowlist ever widens. A new local file, such as a secret or a data dump,
stays out unless someone deliberately allows it. This was checked by listing the context with
decoy files present, and by searching each built image for tests, fixtures, and `.env` files.

### arm64

- Nothing in the build is specific to one architecture:
  - Both base images publish `linux/amd64` and `linux/arm64`.
  - uv is copied from the image for the platform being built.
  - Every third-party dependency installs from a prebuilt wheel, so no compiler is needed.
- **Every compiled dependency has a `manylinux` aarch64 wheel for CPython 3.12** in the locked
  versions. This was checked in two ways. First, each service's dependency closure in `uv.lock` was
  walked. Second, each service's locked dependencies were installed for aarch64 on this machine
  with `uv pip install --python-platform aarch64-manylinux_2_28 --only-binary :all:`. All five
  succeeded, no package needed a source build, and every native library was an ARM aarch64 ELF
  file.

  | Package | aarch64 wheel | Used by |
  |---|---|---|
  | confluent-kafka 2.15.1 | cp312 manylinux_2_28 | all |
  | psycopg-binary 3.3.6 | cp312 manylinux_2_28 | all |
  | pydantic-core 2.46.5 | cp312 manylinux_2_17 | all |
  | pymongo 4.18.2 | cp312 manylinux_2_17 | all |
  | sqlalchemy 2.1.3 | cp312 manylinux_2_17 | all |
  | markupsafe 3.0.4 | cp312 manylinux_2_17 | all (via alembic and mako) |
  | uvloop 0.23.0, httptools 0.8.0, websockets 17.2, pyyaml 6.0.3 | cp312 manylinux_2_17 | api |
  | watchfiles 1.3.0 | abi3 manylinux_2_17 | api |

  Debian trixie has glibc 2.41, which is newer than every one of these wheels requires.
- **Phase 7 builds each architecture natively.** CI builds amd64 on `ubuntu-24.04` and arm64 on
  `ubuntu-24.04-arm` runners, which are free for public repositories. It pushes each build to GHCR
  by digest, then joins the two into one multi-arch tag. The cluster pulls `linux/arm64`
  automatically. No emulation is needed anywhere.

### Measurements (amd64)

| Image | Compressed (download) | Unpacked |
|---|---|---|
| Base `python:3.12.15-slim-trixie` (shared) | 43 MB | 134 MB |
| `filings-api` | 98 MB | 305 MB |
| `filings-discovery`, `-parser`, `-summarizer`, `-notifier` | 89 MB each | 274 MB each |

- **Base layers are shared**, so a node downloads the base once, then about 46–55 MB per service
  image.
- **Largest packages in the venv:**
  - SQLAlchemy: 28 MB
  - botocore: 22 MB
  - psycopg-binary (bundled libpq and OpenSSL): 20 MB
  - uvloop (API only): 17 MB
  - librdkafka: 12 MB
  - Compiled bytecode adds about 32 MB in total.
- **Rebuild times:**
  - A code-only change rebuilds an image in about 14 s, because the dependency layer is cached.
  - The other images stay fully cached.
  - All five images build in under a minute with a warm uv cache.
- **Smoke tests:**
  - Each image imports its package and runs its command's `--help`. The consumers have no
    `--help`, so they were checked to start and stop at settings validation.
  - The API image, attached to the Compose network, answered `/healthz`, `/filings`,
    `/companies`, and `/search` with 200.
  - Migrations from the discovery image brought a throwaway database to head.
  - The relay ran against Redpanda's internal listener.
  - The API and the relay each stopped cleanly on SIGTERM in under a second.

## Alternatives considered

- **A Dockerfile per service.** Five nearly identical files would drift apart. With one file every
  service builds the same way, and adding a service means adding a two-line stage.
- **Selecting the command with `--target` instead of `PACKAGE`.** The target and the installed
  package would then be two inputs that must match. Selecting the final stage from `PACKAGE` keeps
  it to one.
- **Alpine (musl) base.** confluent-kafka and pymongo publish no musl wheels for aarch64, so they
  would compile from source (confluent-kafka against librdkafka). musl also has known performance
  and DNS differences for Python. Debian slim with `manylinux` wheels is the safe choice.
- **Distroless Python images.** Their Python version follows Debian's, not our 3.12. They also
  have no shell, which makes debugging a pod harder for little gain at this size.
- **uv's Python-plus-uv images as the runtime.** That would ship uv to production for no reason.
  Copying the uv binary into the builder stage of the official Python image also guarantees the
  builder and runtime have the same interpreter at the same path.
- **arm64 builds under QEMU emulation.** This works on any amd64 machine, but builds run several
  times slower, and it needs host binfmt changes. Native arm64 runners are free once the
  repository is public.
- **Cross-installing aarch64 wheels on an amd64 builder** (`uv ... --python-platform`). This
  avoids emulation, but the runtime stage's `RUN` steps still execute target-architecture binaries.
  It is also harder to reason about than a native build.

## Consequences

- **Version pins live in two places.** A Python upgrade changes `.python-version` and
  `PYTHON_IMAGE`. A uv upgrade in development should also bump `UV_IMAGE`.
- **The venv is one layer.** Any code change produces a new venv layer, about 45–55 MB
  compressed, to push and pull. Splitting third-party packages and our own code into separate
  runtime layers is possible if deploy bandwidth ever matters.
- **The image is read-only to the app.** Kubernetes can set `readOnlyRootFilesystem`. The local
  object store backend (`data/objects` under `/app`) needs a volume, but production uses R2.
- **Signal handling as PID 1.** The long-running processes (API, parser, summarizer, notifier,
  relay) handle SIGTERM themselves. `filings-discovery`, `filings-replay`, and `filings-index`
  don't. Python running as PID 1 ignores an unhandled SIGTERM, so Kubernetes kills those after the
  grace period. That is safe: each filing's writes are one transaction, and re-runs are idempotent.
  If faster shutdown ever matters, add a SIGTERM handler or a tiny init process such as tini.
- **Not yet proven on arm64 or as a full pipeline.** The first real arm64 builds happen in
  Phase 7's CI, and containerized services first run together on k3s. Per the wheel check, the
  risk is low. App services stay out of `docker-compose.yml` on purpose: during development they
  run on the host (ADR-0003).
- **The Dockerfile frontend is fetched.** The `# syntax=docker/dockerfile:1` line has BuildKit
  fetch the current stable 1.x Dockerfile frontend (cached). The file relies on newer features:
  `COPY --parents` and `${PACKAGE#filings-}`.

## Tools in plain language

- **Multi-stage build:** one Dockerfile with several `FROM` stages. Early stages hold the tools
  needed to build (here, uv). The final image copies in only the result (the virtualenv), so
  build tools never ship.
- **uv in Docker:** the same tool and lockfile as development install the dependencies inside the
  image. Containers therefore run exactly the versions CI tested. Installing dependencies before
  copying the code lets Docker reuse that slow step when only code changed.
- **Why one parameterized Dockerfile:** the five services share a base, a dependency mechanism,
  and a runtime setup. They differ only in which package is installed and which command runs. A
  build argument expresses that difference, so a fix to the build applies to every service.
- **Non-root user:** if an attacker gets code execution in a container, running as an
  unprivileged user that can't write to the image limits what they can do. Kubernetes can refuse
  to start containers that would run as root.
- **BuildKit cache mount:** a folder that persists between builds but never becomes part of the
  image. Here it holds uv's download cache.
- **Multi-arch image:** one tag that points at several builds, one per CPU architecture. Each
  machine pulls the build that matches it. Phase 7 builds each architecture natively in CI and
  combines them under one tag.
- **`.dockerignore`:** decides which files are sent to Docker when building. Here it is an
  allowlist, so only the files the build needs leave the machine.
