# ADR-0010: Local LLM in a Compose profile, and the default model

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

ADR-0008 puts a local, OpenAI-compatible model first in the 8-K summarizer's failover chain, with
Gemini as the fallback. Two things were still open:

- **Where the local model runs in development.** Ollama installed on Windows is not reachable from
  WSL by default; it needs `OLLAMA_HOST=0.0.0.0` or WSL's mirrored networking. Anyone cloning the
  repository would also need their own Ollama setup.
- **Which model to use by default.** The brief (§12) asks for a reasonable default now, decided
  properly by the Phase 8 benchmark. The memory budget allows a 3B-class model at 4-bit, about
  2–3 GB.

## Decision

- **Ollama runs as an optional Compose service** (profile `llm`, image `ollama/ollama:0.40.0`,
  amd64 and arm64).
  - `make up` is unchanged. `make llm-up` starts Ollama and pulls the default model, and
    `make llm-down` stops it.
  - It listens on the default `LLM_BASE_URL`, so no other configuration is needed.
  - A host install remains usable by pointing `LLM_BASE_URL` at it.
- **`OLLAMA_CONTEXT_LENGTH=8192`.** Prompts reach about 6k tokens, and Ollama's default context
  window would silently cut them off. Ollama is also capped at one loaded model and one request at a
  time, with a 5 GB memory limit.
- **Default model: `llama3.2:3b`** (2.0 GB), chosen with a small benchmark through the production
  provider, prompt, and grounding check, on five varied real 8-Ks, on CPU (laptop i7-13620H):

  | Model | Avg / slowest per 8-K | Valid JSON | Filings with grounding flags |
  |---|---|---|---|
  | llama3.2:3b | 41.5 s / 62 s | 5/5 | 1 (a computed total, as Gemini also does) |
  | granite4:3b | 56.3 s / 106 s | 5/5 | 0 |

  Llama is about a quarter faster and wrote the better headlines. Granite gave more detail on the
  earnings release, but missed the point of one headline ("announces 8-K filing on ...").

## Alternatives considered

- **Require the Windows Ollama install.** It needs a manual networking change per machine and
  doesn't help anyone cloning the repository.
- **granite4:3b as the default.** It produced no grounding flags, but it is slower, and its headline
  quality varied between runs.
- **A larger model (7B+).** It doesn't fit the production memory budget next to everything else on
  the 12 GB VM.

## Consequences

- **The benchmark found a limit that grounding can't catch.** On Nike's shareholder-vote 8-K, both
  small models claimed that all proposals passed, but a shareholder proposal failed. The grounding
  check only covers numbers, so semantic errors like this remain possible. The Phase 8 benchmark
  needs a quality measure beyond grounding (for example, reference summaries for a fixed set of
  filings). It may also lead to putting Gemini first for complex filings, which would need a
  configurable provider order.
- **Speed.** Local summaries take 30–60 s per 8-K on the laptop CPU and will be slower on the
  2-OCPU arm64 VM. At about 20 8-Ks a month, that is fine.
- **Live failover test** (Phase 2's done condition), with 20 real 8-Ks replayed through the
  summarizer (local model first, then Gemini):
  1. The local model summarized 3 filings.
  2. The Ollama container was stopped mid-run. The next call failed over to Gemini, and after
     3 consecutive failures the breaker opened, so later filings went straight to Gemini.
  3. Ollama was restarted. One trial call after the 60 s cooldown arrived just before Ollama was
     back and reopened the breaker; the next trial succeeded, and the local model took the last 6.

  Totals: 9 local, 11 Gemini. No dead letters, no lost or duplicated announcements (the
  `filings.ready` count was unchanged), and every 8-K is `ready`. Grounding flags: 1 of 9 local
  summaries, 2 of 20 Gemini summaries, all of them computed totals.
- **A second semantic slip.** In the same run, the local model wrote "Oracle Corp Cancels Larry
  Ellison's Plan to Sell Oracle Stock", but it was Ellison who cancelled his own trading plan, as
  Gemini correctly said.
