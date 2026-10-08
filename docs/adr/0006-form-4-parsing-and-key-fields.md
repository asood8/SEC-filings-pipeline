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

### Transaction lines in Postgres (added 2026-10-08)

The per-transaction table deferred below now has features that need it: the filing page shows
every trade, and `GET /companies/{ticker}/insider-activity` totals insiders' purchases and sales.

- **`form4_transactions`:** one row per transaction line, from both tables (stock and
  derivatives), keyed by (accession number, line). Lines are numbered from 1 in the payload's
  order: stock first, then derivatives, each as filed. The columns are what's worth querying:
  derivative or not, security title, transaction date, code, shares, price per share (NULL when
  footnote-only), acquired or disposed, shares owned after, and direct or indirect ownership.
  Holdings rows, footnotes, the nature of indirect ownership, and derivative terms (exercise
  price, dates, underlying security) stay in MongoDB only.
- **Postgres for querying, MongoDB for the full payload.** The API reads only Postgres, and the
  insider view is an aggregate over many filings, which is SQL's job. MongoDB stays the complete
  record, and the table can always be rebuilt from it.
- **Written with the key fields.** The parser's `save_key_fields` upserts the `form4_filings` row
  and replaces the filing's lines (delete, then insert) in the same transaction that moves the
  filing to `parsed`.
  - Replaying a filing leaves exactly the same rows; a reparse that finds fewer lines leaves none
    behind.
  - The upsert locks the `form4_filings` row until commit, so two handlers rewriting one filing
    take turns.
  - The foreign key points at `form4_filings`, which points at `filings`, both `ON DELETE
    CASCADE`: lines never exist without their filing's key fields.
- **Backfill without SEC:** `filings-rebuild-key-fields [--form-type 4] [--accession ...]`
  rewrites the key fields of parsed filings from their stored payloads. It reads no raw documents,
  publishes no events, and leaves statuses alone. A payload from another parser version is skipped
  (reparse it with `filings-replay discovered`), since its meaning may differ. On a copy of the
  local database it wrote 646 lines (108 of them derivatives) for 256 Form 4s in about a second; a
  second run changed nothing.
- **What counts as insider buying and selling:** codes P and S (open-market or private purchases
  and sales) on stock lines. Grants, exercises, tax withholding, and gifts are left out: they are
  compensation mechanics, not decisions to buy or sell. Lines are grouped by the filing's list of
  reporting owners, so joint filers (Berkshire Hathaway and Warren Buffett) count once. Value is
  shares times price over the lines with a price; shares without one are reported separately. The
  window is 1 to 365 days of transaction dates, on `issuer_cik`.

Alternatives considered for this change:

- **The API reading MongoDB** for the filing page. Fine for one filing, but the insider totals
  would need MongoDB aggregation pipelines, and the API a second datastore on every request.
- **Upserting each line** by (accession number, line). A reparse with fewer lines would leave
  stale ones behind.
- **One row per owner** (each joint filer gets every line). It double-counts joint filings.

Consequences: an existing database needs the migration, then one `filings-rebuild-key-fields
--form-type 4` run. Owner names are shown as filed ("COOK TIMOTHY D"), and an owner who files
alone and jointly gets two rows; owner CIKs aren't stored yet. Activity totals add shares across
share classes (Lennar's Class A and B); values are unaffected. (Owner CIKs and readable names were
added the same day; see below.)

### Owner CIKs and readable names (added 2026-10-08)

- **`form4_filings.reporting_owner_ciks`:** the owners' CIKs, in the order of
  `reporting_owners` (their names as filed). The parser writes both. Rows saved before the
  column existed have NULL until `filings-rebuild-key-fields --form-type 4` fills them from
  MongoDB.
- **Insider activity groups filings by the set of owner CIKs**, not by the list of names. The
  same owners in another order, or with a name typed differently, now share a row. A joint
  filing is still one row for its owners together, and its lines count once. An owner who also
  files alone still gets a row of their own: splitting a joint filing's shares between its owners
  would be a guess. A row is named as on its owners' latest filing. Rows without CIKs fall back
  to the set of names.
- **Readable names:** the API returns each owner as `{cik, name, display_name}`, where `name` is
  as filed and `display_name` is `filings_common.names.display_name` ("Timothy D Cook";
  entities such as "Berkshire Hathaway Inc" keep their word order). The website shows the
  display name, with the name as filed on hover. The Form 4 template uses the same function.
  It reorders "LAST FIRST MIDDLE" by rule (particles such as "Di Sibio" and suffixes such as
  "Jr" and "III" are handled), and checked on all 193 owner names in the local database, it got
  191 right. The two it gets wrong, a two-word last name with no particle ("MONTGOMERY RICE
  VALERIE MD") and a name filed first name first ("SACHIN J. MEHRA"), are why the name as filed
  stays in the API.

Alternatives considered: grouping by CIK in SQL (`GROUP BY` a sorted array) works too, but the
rows need the latest filing's names as well, which is simpler in Python over one row per filing;
and storing display names would freeze today's rules into the data, while computing them per
request costs microseconds.

### Insider trades across companies (added 2026-10-08)

`GET /insider-trades` and the website's Insider trades page rank the largest recent purchases
or sales across every company's stock: the "largest insider purchases this week" feature that
the line table was waiting for.

- **One row per Form 4.** A filing's lines add up to one trade: all its P lines on the purchase
  side, all its S lines on the sale side (a filing with both appears on each). A joint filing
  is one row, so its lines count once. Rows aren't grouped by owner: the page is about single
  trades, and the company page already totals each owner's.
- **The same rules as insider activity:** stock lines with code P or S, a window of 1 to 365
  days on transaction dates (each line on its own, so a filing that straddles the window counts
  only its lines inside it), and value over the priced lines. A company's rows therefore add up
  to its insider-activity totals. On the local database (90 days: 6 purchase filings, 46 sale
  filings), all 21 company totals agree to the cent.
- **Ranking:** by value, largest first. A filing with no price for any line comes last, since
  its value isn't known, and equal values put the latest trade first.
- **Bounded by filing date.** A trade is reported after it happens, so the query also asks for
  filings filed within the window (`filings.filed_at`, which the feed's index covers) and reads
  only their lines, through the partial index on the lines it totals. The work then follows
  `days`, not the whole history. `period_of_report` can't bound it: it is the earliest trade,
  and a late Form 4 can report old lines next to new ones (0002032089-26-000004, filed
  2026-09-11, reports gifts from 2025-09-22 to 2026-09-09). No live trade is dated after its
  filing.
- **The issuer's ticker comes from the companies table** (SEC's ticker list), not from the Form
  4. The filer types its own, which can be "BRK.A" or "LEN, LEN.B"; the companies table's is the
  one the company pages take. An issuer missing from that table gets no ticker, so no link.
- **No owner titles yet.** The owners' relationship (director, officer and title, 10% owner) is
  parsed but stored only in MongoDB. Showing it needs a `form4_filings` column and a
  `filings-rebuild-key-fields` backfill. Until then, the filing page the row links to has the
  role in its summary ("Timothy D Cook (Executive Chair, Director) reported ..."). (Added the
  same day; see "Owner relationships" below.)

### Owner relationships (added 2026-10-08)

The insider pages now say who each insider is to the company: "Mark A Stevens (Director)",
"Amy Hood (EVP, CFO)", "Berkshire Hathaway Inc (10% owner)".

- **`form4_filings.reporting_owner_relationships`** (JSONB): an array in the order of
  `reporting_owners`, one object per owner with the Form 4's boxes and texts as filed:
  `{is_director, is_officer, is_ten_percent_owner, is_other, officer_title, other_text}`
  (`filings_common.parsed.OwnerRelationship`). A check constraint keeps one per owner
  (`jsonb_array_length(...) = cardinality(reporting_owners)`, which also rejects a value that
  isn't an array). The parser writes it with the other key fields; rows saved before have NULL
  until `filings-rebuild-key-fields --form-type 4` fills them from MongoDB. On a copy of the
  local database that took about 5 seconds for 261 Form 4s, and a second run left the column
  byte-for-byte the same.
- **One JSON array rather than parallel arrays or a table.** An owner's relationship stays in one
  piece, there is one column to keep in step with the names, and the API validates each object
  with the same model the parser dumps. Filtering by role stays possible
  (`reporting_owner_relationships @> '[{"is_officer": true}]'`, which a GIN index can serve when
  a feature needs it). Six parallel arrays (four of booleans, two of nullable text) would each
  have to be kept in step. A `form4_reporting_owners` table is the normalized design, but names
  and CIKs are already arrays here, and every reader wants all of a filing's owners at once:
  moving all three is a larger change that no feature needs yet.
- **Stored as filed, labelled per request,** like display names: changing the label needs no
  backfill. `filings_common.names.relationship_label` builds it: the officer title first, as the
  Form 4 template's summaries write it, then "Director", "10% owner", and the other text
  ("President and CEO, Director"); "Officer" or "Other" when the box has no text; null when no
  box is ticked. `names.officer_title` abbreviates exactly the officer titles that search
  already treats as synonyms (CEO, CFO, COO, CTO, CAO, EVP, SVP, VP: `search.ABBREVIATIONS` now
  starts with `names.OFFICER_ABBREVIATIONS`), collapses runs of spaces, and title-cases a title
  typed in capitals, keeping words of up to three letters or without a vowel as abbreviations
  ("CEO, UHG"). Anything else stays as typed: filers cut titles at 30 characters ("EVP, Chief
  Human Resources Off"), and expanding them would be guessing.
- **API:** every reporting owner object (filing detail, insider activity, insider trades) has
  `relationship` (the label) and the six fields as filed; all null on a row not yet rebuilt.
  Insider activity labels a row's owners as on their latest filing, like their names. A
  relationship belongs to a filing, so to one issuer: Tim Cook (CIK 1214156) is "Executive Chair,
  Director" on Apple's Form 4s and "Director" on Nike's.
- **Website:** after each owner's name, the label in muted text, with the officer title as filed
  on hover when the label shortens it. `OwnerNames` now renders one element, so in the phone
  layout, where table cells are flex rows, names and labels wrap as one line of text.

On the local data (2026-10-08: 261 Form 4s, 266 owners, 5 joint filings), every owner ticks at
least one box: 127 are directors only, 107 officers only, 21 both, 10 ten-percent owners only
(Berkshire Hathaway and Warren Buffett, Bank of America), and 1 all three (Mark Zuckerberg,
"COB and CEO, Director, 10% owner"); none is "other". Of 129 officer titles (65 distinct),
`officer_title` changed 56: 18 distinct, such as "Executive Vice President" to "EVP" and
"Senior Vice President,  CFO" to "SVP, CFO". Before the backfill the API returns every owner
with null relationship fields and the website shows names alone, as before.

Alternatives considered: storing the label (it would freeze today's wording into the data);
reading MongoDB from the API (a second datastore on every insider request, as above); dropping
"Director" when the title already says Chair ("Chair and CEO, Director"): shorter, but not every
chair title is a board seat, so the label keeps what the filing says.

## Alternatives considered

- **A per-transaction relational table** (`insider_transactions`). It is the natural next step for
  features such as "largest insider purchases this week", but nothing needs it yet; the full detail
  is in MongoDB. Add it when a feature does. (Added on 2026-10-08 as `form4_transactions`; see
  "Transaction lines in Postgres" above.)
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
