# ADR-0005: Use httpx2 instead of httpx

- **Status:** Accepted (amends the HTTP row of [ADR-0004](0004-service-runtime-libraries-and-patterns.md))
- **Date:** 2026-10-05

## Context

ADR-0004 chose httpx for HTTP: the EDGAR client, and the test client FastAPI tests use. httpx has
seen little maintenance recently. Starlette 1.7, which FastAPI builds on, now deprecates using httpx
in its test client and recommends httpx2.

## Decision

We will use **httpx2**: Pydantic's maintained fork of httpx, forked from httpx 0.28.1.

- The migration is a rename: `import httpx` becomes `import httpx2`. No other public API changed,
  so the EDGAR client and its mock-transport tests carry over unchanged.
- httpx is no longer a dependency of any package in the workspace.

## Alternatives considered

- **Stay on httpx.** It works today, but a library on the critical path of every SEC request should
  receive security fixes. It also keeps a deprecation warning in every test run.
- **requests.** No built-in mock transport, and no async API should one ever be needed.

## Consequences

- httpx2 verifies TLS against the **operating system's trust store** (through `truststore`), not
  certifi's bundled certificates. Production images must include CA certificates (the
  `ca-certificates` package on Debian-based images). Check this when the images are built in
  Phase 7.
- httpx2's default User-Agent is `python-httpx2/<version>`. That doesn't affect SEC calls: the
  EDGAR client always sets the declared User-Agent itself.

## Tools in plain language

- **httpx2:** a Python HTTP client, the maintained continuation of httpx. It makes the web
  requests, here mainly to SEC EDGAR.
