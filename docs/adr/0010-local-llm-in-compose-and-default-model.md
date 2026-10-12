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
  window would silently cut them off. Ollama is also capped at one request at a time and two
  loaded models (the LLM and, since ADR-0011, the embedding model), with a 5 GB memory limit.
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
- **The default model's quality, measured (2026-10-10).** `filings-benchmark` with prompt v1 on
  all 22 labeled 8-Ks (ADR-0008): fact recall 86%; 5 summaries with a claim the filing
  contradicts (Oracle's actor, ExxonMobil's issuer, Tesla's $14.0 billion of commitments, Chevron's
  Nelson "stepping down", PepsiCo's year-to-date growth rate given as the quarter's); 2 with
  numbers the filing doesn't contain; median 46 s, p95 109 s. The 5 GB limit was hit once: the
  kernel killed Ollama's runner on Oracle's long results 8-K, so that filing failed. Ollama
  restarted the runner, and the filing then summarized in 25 s on its own. The 2026-10-08 run hit
  it too, so a long 8-K can need more than 5 GB at an 8k context; in the pipeline, that costs a
  failover to Gemini. *Corrected 2026-10-11:* it wasn't the long 8-K. The llama-server that Ollama
  runs kept earlier prompts in RAM for reuse, 20 of them and 3.5 GiB by then, on top of the
  model. Compose now turns that off (`LLAMA_ARG_CACHE_RAM=0`, which llama-server reads from the
  environment Ollama passes on), and production's llama.cpp server gets `--cache-ram 0`
  (ADR-0017, where the same server was killed after six 8-Ks at its 3 GiB limit).
- **A prompt experiment, not kept (2026-10-09).** With summary quality now measurable (fact recall
  and forbidden claims against labeled 8-Ks, ADR-0008), prompt v2 added five rules aimed at the
  slips in the [audit](../8k-summary-audit.md): cover every event the items report, not just the
  main one; name whoever acted, as the filing does; keep each figure's label and period (a
  quarter is not year to date, a draw period is not a loan's term, an optional increase is not a
  current amount); give vote outcomes from the vote counts; no key points about the filing itself
  (exhibit numbers, where documents can be found). Measured with llama3.2:3b against v1 on the 9
  8-Ks with known errors, twice each:

  | Prompt | Summaries | Failed | Median s | Fact recall | With a forbidden claim | With ungrounded numbers |
  |---|---|---|---|---|---|---|
  | v1 (kept) | 18 | 0 | 30.3 | 82% | 50% | 11% |
  | v2 | 18 | 1 | 33.5 | 83% | 47% | 6% |

  As first scored, v2 cut summaries with a forbidden claim from 33% to 12%. Reading them showed
  that most of the drop was the same errors in words the labels' patterns didn't anticipate
  (Chevron's Nelson "will leave his role as ... and Vice Chairman"; PepsiCo's "organic revenue
  increased 3.1% to $68,898 million", which is year-to-date net revenue), plus one new error
  (Gustavson, not Bonner, "will become President, Oil, Products & Gas"). Once those were added
  to the labels, the two prompts were one summary apart. Oracle's wrong actor and ExxonMobil's
  wrong issuer survived both. v2 also cost a schema failure (Tesla's key points broke into 14
  fragments, where the schema allows 8) and about 10% more time, so v1 stays. The lessons: a 3B
  model doesn't follow a list of rules reliably, and the errors left are about who did what,
  which may need a larger model or Gemini first for complex 8-Ks; and a better score needs
  reading before it is believed, since patterns only catch the wordings someone thought of.
