# syntax=docker/dockerfile:1
# One image per service, all from this file. PACKAGE picks the workspace package to install and the
# image's default command (`make images` builds them all):
#
#   docker build -f docker/service.Dockerfile --build-arg PACKAGE=filings-api -t filings-api:dev .
#
# PACKAGE: filings-api, filings-discovery, filings-parser, filings-summarizer, or filings-notifier.
# Every image also has the filings-common commands (filings-outbox-relay, filings-replay,
# filings-index). Builds for linux/amd64 and linux/arm64. See docs/adr/0014-container-images.md.

# Keep in step with .python-version and the uv version used in development.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.23
ARG PACKAGE

FROM ${UV_IMAGE} AS uv

# Build: install the package and its locked dependencies into a virtualenv at /opt/venv.
FROM ${PYTHON_IMAGE} AS builder
ARG PACKAGE
COPY --from=uv /uv /usr/local/bin/uv
# Bytecode is compiled here, once, so containers start faster. The uv cache is a mount on another
# filesystem, so packages are copied out of it rather than hard-linked. The base image's Python is
# used; uv never downloads one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

# Third-party dependencies first, from the lockfile and package manifests only, so this layer is
# rebuilt when a dependency changes, not on every code change. --locked fails if uv.lock is stale.
COPY --parents pyproject.toml uv.lock libs/*/pyproject.toml services/*/pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --no-install-workspace --package "$PACKAGE"

# Then the code: the shared library and this one service, so a change to another service doesn't
# rebuild this image. --no-editable installs real copies, so the venv doesn't need /src at runtime.
COPY libs/ libs/
COPY services/${PACKAGE#filings-}/ services/${PACKAGE#filings-}/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package "$PACKAGE"

# Runtime: the same Python base image plus the venv. No uv, no source tree, no build tools.
FROM ${PYTHON_IMAGE} AS runtime
# A numeric UID lets Kubernetes check runAsNonRoot without looking up a user name.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# Relative paths in settings (config/watchlist.toml, data/objects) resolve here. Everything in the
# image is owned by root, so the app can't modify its own code: mount a volume for what it writes.
WORKDIR /app
USER 10001:10001

# Default command per package. The last stage selects one of these by PACKAGE.
FROM runtime AS cmd-filings-api
EXPOSE 8000
CMD ["uvicorn", "--factory", "filings_api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS cmd-filings-discovery
# Public configuration, not a secret. Mount a file over it (a ConfigMap) to change it without a
# rebuild, or point DISCOVERY_WATCHLIST_PATH elsewhere.
COPY config/watchlist.toml config/watchlist.toml
CMD ["filings-discovery", "run"]

FROM runtime AS cmd-filings-parser
CMD ["filings-parser"]

FROM runtime AS cmd-filings-summarizer
CMD ["filings-summarizer"]

FROM runtime AS cmd-filings-notifier
CMD ["filings-notifier"]

# An unknown PACKAGE fails here: Docker looks for an image named cmd-<PACKAGE> and can't pull it.
FROM cmd-${PACKAGE:?build_with_--build-arg_PACKAGE=filings-SERVICE}
ARG PACKAGE
LABEL org.opencontainers.image.title="${PACKAGE}" \
      org.opencontainers.image.source="https://github.com/asood8/SEC-filings-pipeline" \
      org.opencontainers.image.licenses="MIT"
