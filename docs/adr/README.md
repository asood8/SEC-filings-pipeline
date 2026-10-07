# Architecture decision records

An ADR is a short document that records one significant decision: its context, what was decided,
the alternatives, and the consequences. Accepted ADRs are not rewritten. When a decision changes,
a new ADR supersedes the old one, and the old one's status is updated to point at it.

To add one, copy [`0000-template.md`](0000-template.md) to the next number, fill it in, and add a
row below.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-architecture-overview.md) | Architecture overview | Accepted (frontend superseded by 0015) |
| [0002](0002-python-monorepo-and-tooling.md) | Python monorepo and tooling | Accepted |
| [0003](0003-local-development-stack.md) | Local development stack | Accepted |
| [0004](0004-service-runtime-libraries-and-patterns.md) | Service runtime libraries and patterns | Accepted |
| [0005](0005-httpx2-instead-of-httpx.md) | Use httpx2 instead of httpx | Accepted |
| [0006](0006-form-4-parsing-and-key-fields.md) | Form 4 parsing and key fields | Accepted |
| [0007](0007-8k-items-summaries-and-replay.md) | 8-K item extraction, summary storage, and replay | Accepted |
| [0008](0008-llm-providers-failover-and-exhibits.md) | LLM providers, failover, and 8-K exhibits | Accepted |
| [0009](0009-grounding-check-for-llm-summaries.md) | Grounding check for LLM summaries | Accepted |
| [0010](0010-local-llm-in-compose-and-default-model.md) | Local LLM in a Compose profile, and the default model | Accepted |
| [0011](0011-search-and-public-api.md) | Search and the public API | Accepted |
| [0012](0012-notifier-and-exactly-once-alerts.md) | Notifier and exactly-once alerts | Accepted |
| [0013](0013-sign-in-and-sessions.md) | Sign-in with Google and sessions | Accepted |
| [0014](0014-container-images.md) | Container images for the services | Accepted |
| [0015](0015-frontend-stack.md) | Frontend stack: Vite, React, and TypeScript | Accepted |
| [0016](0016-observability.md) | Observability with OpenTelemetry | Accepted |
