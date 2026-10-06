# ADR-0007: 8-K item extraction, summary storage, and replay

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

Phase 2 summarizes filings. Before any LLM is involved, four things need settling:

- how 8-Ks are split into their numbered items;
- how summaries are stored, so that re-summarizing is safe;
- how Form 4s are summarized;
- how history is replayed after a parser, prompt, or model change.

The 20 real 8-Ks from the watchlist's first discovery run are all inline-XBRL HTML. Item headings
sit in their own block element; references to items inside sentences do not. Two filing agents
open the document with a table of contents. SEC publishes each filing's item list in the
submissions API, which gives an independent check.

## Decision

### 8-K item extraction

- Convert the HTML to text lines with the standard library's `HTMLParser`. Block elements end a
  line, table cells are joined with spaces, and hidden content (the `ix:header` XBRL block,
  `display:none`, scripts, styles) is skipped.
- A line starting with "Item N.NN" starts an item. The cover page before the first item is
  dropped, and so is everything from the signatures block on.
- Page numbers and "Table of Contents" links are dropped, so a table of contents becomes a run of
  headings with no text, and the first run that has text wins.
- Items carry SEC's official titles. Their numbers also go to Postgres (`form8k_filings.items`).
- **Verified:** on all 20 real 8-Ks, the extracted items match SEC's own item lists.

### Summaries

- **One shape for every summarizer:** `FilingSummary`, with a headline (at most 200 characters), a
  summary, and up to 8 key points.
- **Every summary records its source:** provider, model, and prompt (or template) version.
- **History is kept.** `summaries` is upserted on (filing, provider, model, prompt version), so a
  redelivery replaces its own row and a new prompt or model adds a row. The newest row is current.
- **Announced once.** The summarizer moves a filing from `parsed` to `ready` with a conditional
  update, and writes the `filings.ready` event (which points at the summary row) in the same
  transaction, only when that update happens. A replay can refresh summaries, but it never
  announces a filing again, so the notifier can't send duplicate alerts.

### Form 4 summaries are a template, not an LLM

A Form 4 is already structured data, so a deterministic template is faster, free, and exact.

- Transactions are grouped by transaction code, direction, and security, and each group becomes
  one sentence.
- People's names are shown "First Last" (SEC lists them "LAST FIRST"); entity names stay as filed.
- Share classes and holding entities are never added together.
- An exercise's price is the strike price, so it is never shown as a value.
- Footnote-only prices stay unknown.

### Forms with no summarizer

A form without a summarizer stays `parsed`, and the message is acknowledged rather than
dead-lettered. This mirrors what the parser does for forms it can't parse.

### Replay

`filings-replay discovered | parsed` re-announces filings from Postgres through the outbox,
filtered by form type and status. It inherits the outbox's at-least-once delivery, and replays are
safe because every consumer is idempotent. For `parsed`, the parser version comes from MongoDB.

The payload models (`filings_common.parsed`) live in the shared library: the parser writes them and
the summarizer reads them, so they are a contract, just like the events.

## Alternatives considered

- **An HTML library (BeautifulSoup, lxml) for 8-Ks.** More forgiving of broken markup, but the
  standard library handled all 20 real filings, and adding a dependency isn't justified yet.
- **Using SEC's item list instead of parsing headings.** It is useful as a check, but the text
  under each heading is still needed for summaries.
- **Summarizing Form 4s with the LLM too.** Slower, costs free-tier quota, can hallucinate numbers,
  and adds nothing to data that is already structured.
- **Rewriting a single summary row per filing.** Simpler, but loses the history needed to compare
  prompt and model versions (the Phase 8 benchmark).
- **Replaying by resetting a consumer group.** It works because retention is unlimited, but it
  can't filter by form or status, and it replays everything.

## Consequences

- Earnings 8-Ks (Item 2.02) mostly point to a press release in Exhibit 99.1, a separate file that
  discovery does not download yet. Without it, those summaries will be thin. This is an open
  decision.
- Changing the template's output means bumping `TEMPLATE_VERSION`, then
  `filings-replay parsed --form-type 4`.
- The 8-K LLM summarizer plugs into the same interface and needs no other change.
