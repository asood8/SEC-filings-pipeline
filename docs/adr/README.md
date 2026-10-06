# Architecture decision records

An ADR is a short document that records one significant decision: its context, what was decided,
the alternatives, and the consequences. Accepted ADRs are not rewritten. When a decision changes,
a new ADR supersedes the old one, and the old one's status is updated to point at it.

To add one, copy [`0000-template.md`](0000-template.md) to the next number, fill it in, and add a
row below.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-architecture-overview.md) | Architecture overview | Accepted |
| [0002](0002-python-monorepo-and-tooling.md) | Python monorepo and tooling | Accepted |
| [0003](0003-local-development-stack.md) | Local development stack | Accepted |
| [0004](0004-service-runtime-libraries-and-patterns.md) | Service runtime libraries and patterns | Accepted |
| [0005](0005-httpx2-instead-of-httpx.md) | Use httpx2 instead of httpx | Accepted |
| [0006](0006-form-4-parsing-and-key-fields.md) | Form 4 parsing and key fields | Accepted |
| [0007](0007-8k-items-summaries-and-replay.md) | 8-K item extraction, summary storage, and replay | Accepted |
| [0008](0008-llm-providers-failover-and-exhibits.md) | LLM providers, failover, and 8-K exhibits | Accepted |
| [0009](0009-grounding-check-for-llm-summaries.md) | Grounding check for LLM summaries | Accepted |
