# ADR-0006: Form 4 parsing and key fields

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

Form 4s report insider trades. They are XML `ownershipDocument`s defined by SEC's EDGAR Ownership
XML Technical Specification. A survey of 255 real Form 4s from the watchlist's first discovery run
turned up these facts:

- **Any field can be footnote-only.** Prices, exercise dates, and expiration dates sometimes appear
  only in a footnote (`<footnoteId>`), with no `<value>`. 25 of 641 transactions had no price.
- **Booleans come in two spellings:** `0/1` and `true/false`.
- **The issuer isn't always the watchlist company.** A company's submissions feed also lists Form 4s
  where *it* is the insider. Examples: Berkshire Hathaway buying Lennar; Bank of America holding
  shares in a Nuveen fund.
- **Joint filings list several reporting owners and several signatures.**
- **There are many transaction codes**, not just buys (P) and sells (S): awards (A), option
  exercises (M), tax withholding (F), gifts (G), conversions (C), and others (J, D).

The brief puts the full parsed payload in MongoDB and "key fields" in Postgres.

## Decision

- **Parse with the standard library's ElementTree** into a validated Pydantic model (`Form4`). Every
  value is optional, every row keeps the footnote IDs it cites, and the footnote texts are stored.
  Python's bundled expat rejects entity-expansion attacks, and ElementTree never fetches external
  entities.
- **Keep what the filer reported.** Missing or footnote-only values stay `None` rather than being
  guessed, and a footnote-only price is never filled in from the footnote's text. Interpreting
  footnotes is a job for the summarizer (Phase 2).
- **Quantities are floats.** Shares and prices are reported figures for display and analysis, not
  accounting balances. Floats keep them numeric and queryable in MongoDB.
- **MongoDB holds the full payload** (`parsed_filings`, keyed by accession number), stamped with a
  `parser_version` (currently 1) so outdated parses can be found and redone.
- **Postgres gets a `form4_filings` row per filing** with the fields worth querying and joining:
  - period of report;
  - the issuer's CIK, name, and ticker;
  - reporting owner names;
  - distinct transaction codes;
  - the 10b5-1 flag.

  It is upserted inside the same transaction that moves the filing to `parsed`. `issuer_cik` has no
  foreign key, because issuers are not always in SEC's ticker map.
- **Forms without a parser** (8-K until Phase 2) are acknowledged and stay `discovered` instead of
  being dead-lettered. They are expected, not errors, and the dead-letter topics stay meaningful for
  the "DLQ not empty" alert.

## Alternatives considered

- **A per-transaction relational table** (`insider_transactions`). It is the natural next step for
  features such as "largest insider purchases this week", but nothing needs it yet; the full detail
  is in MongoDB. Add it when a feature does.
- **lxml or xmltodict.** lxml is faster but adds a compiled dependency, for no need at these volumes.
  xmltodict yields untyped dicts, where a validated model catches schema surprises.
- **Decimal quantities.** Exact, but MongoDB would need Decimal128 conversions, for no practical gain
  in a summarization pipeline.
- **Dead-lettering unsupported forms.** Simple, but it would fill the DLQ with expected traffic and
  bury real failures.

## Consequences

- Downstream code must handle missing prices and dates, and must use `issuer_cik`, not the filing's
  `cik`, when it means "whose stock was traded".
- A parser change must bump `PARSER_VERSION`; reparsing old filings is a separate job.
- When the 8-K parser lands, the 8-Ks still in `discovered` need re-publishing (part of Phase 2's
  replay tooling).
