# ADR-0002: Python monorepo and tooling

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

There are five Python services (discovery, parser, summarizer, notifier, api) and a shared library
(config, logging, event models, the EDGAR client, ObjectStore, Kafka helpers). The project needs:

- shared code without publishing packages anywhere;
- one command to lint, type-check, and test everything;
- consistent dependency versions across services;
- each service buildable into its own small container image later (Phase 7);
- the same checks locally and in CI.

The brief calls for Python 3.12+, uv, ruff, mypy, and pytest.

## Decision

- **One repository, one uv workspace.** The root `pyproject.toml` is a *virtual* workspace root:
  it is not a package itself. Its members are `libs/*` and `services/*`. There is one `uv.lock` and
  one `.venv`, and `uv sync` installs every member in editable mode.
- **Each member is a src-layout package.** Distribution names are `filings-<name>` and import names
  are `filings_<name>` (`filings_common`, `filings_parser`, ...). The prefix avoids generic
  top-level names such as `parser`, `api`, or `common` that collide with other packages. It is
  neutral because the project name is still undecided.
- **Services depend on `filings-common` through a workspace source**, so a service image can later
  install just that service and its dependencies (`uv sync --package filings-parser`).
- **Python is pinned to 3.12** in `.python-version`. It is the oldest version the brief allows, so
  it has the widest availability of prebuilt arm64 wheels. Moving up is a one-line change plus a CI
  run. uv downloads the interpreter itself, independent of the system Python.
- **Checks:**
  - ruff for linting and formatting;
  - mypy in strict mode;
  - pytest with per-package `tests/` folders. Saved EDGAR documents go in the root `tests/fixtures/`.
  - Test file names may repeat across packages: pytest uses `--import-mode=importlib`, and mypy uses
    `explicit_package_bases` with each package's `src` dir on `mypy_path`.
- **pre-commit** runs file-hygiene checks, private-key detection, ruff, mypy, and a `uv.lock`
  freshness check on every commit. Ruff and mypy run through `uv run`, so their versions come from
  `uv.lock` rather than a second pin in the hooks file.
- **The Makefile is the command entry point** (`make setup`, `lint`, `test`, `up`, ...). CI runs the
  same pre-commit hooks on all files, plus `make test`, after `uv sync --locked`, which fails if
  the lockfile is stale.

## Alternatives considered

- **Separate repositories per service.** Cross-cutting changes (an event schema used by two
  services) would need coordinated releases. That is pure overhead for a solo project.
- **One package with a subpackage per service.** Simpler, but every image would carry every
  service's dependencies, and nothing would stop services importing each other's internals.
- **Poetry, or pip with requirements files.** Poetry's multi-package support is weaker and it is
  slower. Requirements files give no single lockfile across packages.
- **Black, isort, and flake8.** Ruff replaces all three with one fast tool and one config.
- **Ruff and mypy from pre-commit's own hook repos.** That pins their versions in two places, which
  can drift from `uv.lock`.

## Consequences

- Adding a package means adding its `src` dir to `mypy_path` in the root `pyproject.toml`.
- One lockfile means upgrading a dependency upgrades it for every service at once. That keeps
  versions consistent and is accepted.
- `make setup` installs the git hooks; a contributor who skips it is still caught by CI.

## Tools in plain language

- **uv:** a fast Python package and project manager. It installs Python itself, resolves and locks
  dependencies, and manages the virtual environment. A *workspace* lets several packages in one repo
  share a lockfile and environment.
- **ruff:** a linter and formatter. It flags likely bugs and style problems, and rewrites code into
  a consistent format.
- **mypy:** a static type checker. It reads type hints and reports mismatches before the code runs.
- **pytest:** the test runner.
- **pre-commit:** runs a configured set of checks automatically on `git commit`, and blocks the
  commit if one fails.
- **GitHub Actions:** GitHub's CI service. It runs the checks on every push and pull request,
  free for public repositories.
