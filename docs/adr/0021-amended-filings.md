# ADR-0021: Amended filings (Form 4/A, 8-K/A)

- **Status:** Accepted (2026-11-04)
- **Date:** 2026-11-04

## Context

Filers correct a filing by filing an amendment: a Form 4/A for a Form 4, an 8-K/A for an 8-K.
Until now discovery collected only the exact form codes `4` and `8-K`, so every amendment was
dropped. The insider totals (insider activity, the largest trades, alert filters) then kept the
wrong figures the amendment fixed, and the site never showed that a filing had been corrected.

How often, on the 35-company watchlist, over the year to 2026-11-04 (counted from SEC's
submissions API): 13 Form 4/As against about 2,985 Form 4s, and 7 8-K/As against 429 8-Ks.
Reading all 20:

- **The existing parsers read them.** A Form 4/A is the same XML with one more field,
  `dateOfOriginalSubmission`. An 8-K/A has the same items. Many say why they were filed: a Form
  4/A in its remarks or a footnote ("to correct the number of shares acquired, which was
  inadvertently reported as 47,574"), an 8-K/A in an explanatory note or its first item.
- **Form 4/As amend in three ways.** Some restate the whole filing with one figure fixed (Nike's
  corrected price). Some file only the corrected lines (Alphabet's director: the lines of one
  trust's gift, not the other trust's). Some add a trade the original left out (J&J's omitted
  sale, Goldman's 29 restated lines plus 6 new ones). One only attached a power of attorney.
  So neither "the amendment replaces the original" nor "both count" gives the right totals for
  all of them.
- **What links an amendment to its original.** A Form 4/A gives the original's filing date, and
  shares its issuer and a reporting owner. Several Form 4s about one issuer are often filed the
  same day (four Alphabet officers' on 2026-01-15), but the owner, then the lines, tell them
  apart. An 8-K/A names the 8-K's filing date in its text in 6 of the 7 ("...Current Report on
  Form 8-K filed on April 20, 2026"), and shares SEC's report date (the event's date) with it.

## Decision

We will collect amendments and store each as a filing of its own, under its own accession
number, through the same stages as any filing. Then:

1. **Link each amendment to the original** (`filings.amends_accession_no`), always the original
   Form 4 or 8-K, never another amendment, when the parser saves its key fields
   (`filings_parser.amendments`). A Form 4/A's original: a Form 4 about the same issuer, filed
   (or accepted, Eastern time) on the date it gives, sharing a reporting owner; the one sharing
   the most transaction lines if several. An 8-K/A's: the company's 8-K filed on the date its
   text gives, else one with the same report date; one reporting the same item if several.
   Candidates that can't be told apart, or an original that isn't collected, leave the
   amendment unlinked: it is still shown, with the date it gives.
2. **Correct Form 4 lines one by one.** Each amendment line takes the place of the matching
   current line (same transaction date, code, acquired or disposed, security, table, and
   ownership, including how an indirect holding is held), which gets
   `form4_transactions.superseded_by`. Lines the amendment doesn't mention stay; lines with no
   match are new. Totals count only lines not superseded. Amendments apply in the order filed.
3. **Recompute, don't patch.** Saving any Form 4 recomputes the links and marks of its family
   from Postgres, so the result doesn't depend on which filing arrives first, and replays and
   `filings-rebuild-key-fields` change nothing. An advisory lock per company makes saves of the
   same company's filings take turns.

Simulated on all 11 Form 4/As whose original is in SEC's recent filings, the line-by-line rule
gives the corrected figures in every one. The other two: one amends a 2023 Form 4 (unlinked,
its lines count as its own), and Bank of America's fixes "administrative errors" in a filing
about a fund's units with a line that matches none of the original's (other code lines, which
the totals don't count). Its remarks cite the original's accession number; the rules above
don't read it, since every other amendment is found without one.

4. **Summarize amendments as what they are.** A Form 4/A gets the Form 4 template, headed
   "Amended:", and starts with the Form 4 it amends, whether any reported trade changed, and the
   filer's explanation in the filer's words (the sentence of the remarks or footnotes that says
   it amends, or else one naming a mistake in an earlier report). An 8-K/A goes to the LLM with a
   header naming the 8-K it amends, its explanatory note, and one more instruction: say what the
   amendment adds or changes. Its headline is headed "Amended:" too. An 8-K's prompt is
   unchanged, so the benchmark's results for 8-Ks still hold.
5. **Alert amendments like their form.** Form 4 alerts include Form 4/As and 8-K alerts include
   8-K/As, with the same filters, applied to the amendment's own lines and items. A Form 4/A that
   restates its original's trades unchanged (Nike's, attaching a power of attorney) alerts
   nobody, but is still summarized, searchable, and shown.

6. **Show both sides.** A `form_type` filter in the feed, search, and the Atom feed includes
   the form's amendments. An amendment's page says what it amends, with a link (or that the
   original isn't collected); the original's page lists its amendments, and the Form 4 lines an
   amendment restated are marked "Restated by the amendment" with a link (ADR-0011).

### The 8-K/A prompt, measured (2026-11-04)

llama3.2:3b on the laptop, with the production prompt, on the watchlist's 7 8-K/As (labeled in
`config/summary_eval.json` by reading their notes and items): every labeled fact stated (100%
recall), but 4 of 7 summaries make a claim the filing contradicts. Berkshire's amendment, which
only adds Greg Abel's salary, is called a correction. Chevron's sale of the Hess toy-truck
business *to* John Hess is told in reverse. Netflix's headline announces the merger, which the
original announced (the summary itself says correctly that the amendment adds a missing
exhibit). Walmart's says Furner's pay was reported before. Apple's, Walmart's other one, and
McDonald's are right. For 8-Ks the same model makes such a claim in 5 of 22.

A second wording ("summarize what this amendment reports; call it a correction only if the
filing says so") fixed Berkshire's and Walmart's but made three new ones (McDonald's earlier
report "incorrectly stated" a date, Walmart's two-year non-compete became "two months", and
Apple's planned awards "received"), 5 of 7, so the first wording stays. Like the 8-K slips, these
are the small model's; the headline's "Amended:" and the filing's link go with every one.

## Alternatives considered

- **Replace the original with the amendment.** Right for full restatements, wrong for partial
  ones: Alphabet's director would lose the other trust's correct gift, and Alphabet's officers
  their tax-withholding lines.
- **Count both.** Doubles every restated trade: Goldman's 29 lines would count twice.
- **Diff the documents and show what changed.** Useful, but the line-by-line match already says
  which lines changed, and the filer's own explanation says why. Left for later.
- **Ask an LLM which lines changed.** The rules above handle every real case, deterministically
  and for free.

## Consequences

- An amendment is one more filing in the feed, on company pages, and in search, which is what
  SEC shows too.
- A line the original reported in error and the amendment silently drops can't be told from one
  a partial amendment didn't mention, so it stays counted. None of the 13 did this; a filer that
  removes a line usually says so in the remarks, which the summary will show.
- Among several lines with the same key (a day's sales at different prices), an amendment line
  pairs first with an identical one, then with one with the same price or the same shares, then
  in document order. A partial amendment correcting one of many such lines could pick the wrong
  one; the totals still change by the right amount when it corrects the shares of a line with
  its own price.
- Rows stored before this change get `form4_transactions.nature_of_ownership` from
  `filings-rebuild-key-fields --form-type 4`, and `filings.report_date` when discovery next sees
  them (within its lookback window).
- Other forms' amendments (10-K/A and so on) aren't collected, because their forms aren't.
- An amendment found unlinked can't be told apart from a change of figures, so it is alerted
  (rather than missed), with the date it gives for its original.
