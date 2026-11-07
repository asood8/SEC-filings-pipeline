# ADR-0023: Automatic dependency updates with Dependabot

- **Status:** Accepted (2026-11-07)
- **Date:** 2026-11-07

## Context

Every dependency is locked: Python packages in `uv.lock`, the website's in
`web/package-lock.json`, Terraform providers in each root module's `.terraform.lock.hcl`, and
workflow actions pinned to commit SHAs (ADR-0019). The weekly audit (`audit.yml`) reports known
vulnerabilities, but nothing proposed newer versions: they moved only when someone noticed (the
certifi, virtualenv and filelock advisories in October). Locked versions drift further behind
each month, and a big catch-up upgrade is harder to review than small weekly ones.

## Decision

Dependabot version updates, configured in `.github/dependabot.yml`:

- **What:** Python (`uv.lock`, the whole workspace), the website (`web/`), workflow actions (the SHA
  and its version comment), and Terraform providers (`envs/production` and the capacity-report
  tool).
- **When:** weekly, Mondays 07:00 UTC, just after the weekly audit. Minor and patch updates come
  as one grouped pull request per ecosystem, majors one by one, at most 5 open per ecosystem.
- **A week's cooldown:** a release is proposed only once it is 7 days old. Most malicious
  releases of hijacked packages are found and pulled within days, so this keeps them out at
  little cost.
- **Merged by hand:** each pull request runs CI and must pass the same required checks as any
  other; someone reads the release notes before merging (runbook, "Dependency update pull
  requests"). No auto-merge.
- **Not container images.** The images in `docker-compose.yml`, the Kubernetes manifests, and the
  Dockerfile's Python and uv are pinned in several places that must move together, which
  Dependabot can't do, and a data store's upgrade (Postgres, MongoDB, Redpanda) needs its own
  plan. They stay manual (runbook, "Updating a container image").
- `tests/test_dependabot.py` fails if a lock file in the repository has no entry, so a new one
  isn't left out.

### In plain language

Dependabot is a service built into GitHub. Once a week it reads the project's lock files, checks
each package registry for newer versions, and opens a pull request that changes the lock file
(and the version constraint, if needed), with the release notes and changelog in its
description. It needs no install and no secret, and it costs nothing.

## Alternatives considered

- **Renovate.** More flexible: its regex managers could also update the image pins in the
  Kubernetes manifests and the Dockerfile together. But it is a GitHub app to install and grant
  access to the repository, with a larger configuration to learn. Worth revisiting if updating
  images by hand becomes a chore.
- **Dependabot security updates only.** Pull requests only for advisories; the audit workflow
  already reports those, and versions would still drift.
- **By hand**, as until now: updates happened only when something broke or an advisory appeared.

## Consequences

- A few pull requests a week to read and merge; grouping keeps it to about one per ecosystem.
- Dependabot's pull requests get a read-only token and no repository secrets. CI needs neither.
- Images still move by hand; the audit doesn't cover them either (it reads the lock files).
