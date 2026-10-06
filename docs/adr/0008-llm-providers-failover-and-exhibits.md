# ADR-0008: LLM providers, failover, and 8-K exhibits

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

8-Ks are free text, so their summaries need an LLM. The brief sets out:

- a local model first: Ollama in development, a llama.cpp server on the arm64 VM;
- Gemini's free tier as the fallback;
- automatic failover between them, with a circuit breaker and backoff on 429;
- validated, structured output;
- a stored provider, model, and prompt version for every summary.

Two facts surfaced while building it:

- **Gemini's free tier is volatile.** The first call to the newest Flash model returned
  `503 "high demand"`, while `gemini-3.5-flash-lite` answered in about a second.
- **Earnings 8-Ks carry their substance in exhibits.** An earnings 8-K's Item 2.02 mostly says
  "see the press release in Exhibit 99.1", and that press release is a separate file.

## Decision

### Providers

Providers are plain HTTP clients over httpx2, with no vendor SDKs:

- **`OpenAICompatibleProvider`** speaks the OpenAI chat-completions API that Ollama and llama.cpp
  both serve, using `response_format: json_schema`.
- **`GeminiProvider`** calls `generateContent` with `responseJsonSchema`.
  - The key travels in the `x-goog-api-key` header, never in a URL, so it can't leak into logs.
  - Calls are paced by a Redis token bucket shared by all summarizer processes (default
    10 requests/minute, under the free tier's cap).

Both are configured through settings, and each is optional. Leaving `LLM_MODEL` unset means Gemini
only; a blank `GEMINI_API_KEY` means no Gemini. The default Gemini model is pinned to
`gemini-3.5-flash-lite`, not a `-latest` alias, because every summary records its model.

### Failover

`FailoverChain` tries providers in order and gives each one a circuit breaker:

- **Opening:** three consecutive failures open the breaker for 60 s. A 429 opens it for at least
  the retry delay the provider asks for. An answer that fails validation against the
  `FilingSummary` schema counts as a failure, so the next provider gets the request.
- **All providers down:** this raises `LlmUnavailableError`, which is a transient error. The
  summarizer's consume loop waits it out with up to 6 attempts and backoff from 10 s up to 160 s
  (about 5 minutes). The consumer's max poll interval is raised to 15 minutes to match. Meanwhile
  the backlog waits in the topic. If retries run out, the filing is dead-lettered and replay
  recovers it.
- **No open transactions:** no database transaction is held open during an LLM call.

### Prompt

- **Version 1, versioned in code.** The prompt contains the company, the filing date, each item,
  and each EX-99 exhibit. Each section is trimmed at a line boundary: 6k characters per item, 8k
  per exhibit, 24k in total, so it fits a small local model's context. Press releases lead with
  the headline facts, so keeping the beginning keeps what matters.
- **Facts only, as stated.** The instructions forbid outside knowledge, speculation, advice, and
  computed figures.

### EX-99 exhibits

- **What's collected:** for 8-Ks only, discovery reads the filing's `index-headers` page (each
  document's SGML header: type, file name, description) and downloads only EX-99 exhibits in HTML
  or text, meaning press releases and investor materials. Contracts (EX-10), XBRL (EX-101),
  images, and PDFs are skipped.
- **Atomic with the filing:** exhibit rows (`filing_exhibits`) are written in the same transaction
  as the filing and its event. The parser reads them from Postgres, so the event schema is
  unchanged.
- **Into the payload:** 8-K parser v2 adds each exhibit's text to the payload, capped at 100k
  characters to bound MongoDB storage.
- **Backfill:** `filings-discovery fetch-exhibits` covers filings discovered before exhibits were
  collected.

## Alternatives considered

- **Vendor SDKs (openai, google-genai).** More dependencies for two small POST requests, and
  harder to test than httpx2's mock transport.
- **Waiting for a provider in-process instead of raising.** That blocks one message for an
  unbounded time and risks the broker evicting the consumer. Bounded retries in the consume loop
  are explicit and observable.
- **Downloading every exhibit.** Contracts and XBRL are large and add little to a summary.
- **Downloading no exhibits.** Earnings 8-Ks, the most important kind, would get empty summaries.
- **A `-latest` model alias.** The recorded model name would stop identifying what produced a
  summary.

## Consequences

- **Live results.** All 20 real 8-Ks were summarized through Gemini in about 2 minutes at the paced
  rate, with no failures. A check of 156 numbers in the summaries found none false. Two were totals
  the model computed despite the instruction, and small models do this. A deterministic grounding
  check (flag any number that isn't in the source) is the proposed follow-up.
- **Discovery cost.** Each 8-K now costs 1 extra SEC request, plus one per EX-99 exhibit: about
  40–60 a month for the watchlist.
- **Local path not yet exercised live.** The local model is only covered by mock-transport tests
  until Ollama is reachable. Phase 2's "local model stops → Gemini takes over" check waits for
  that.
