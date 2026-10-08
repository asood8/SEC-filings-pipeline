# 8-K summary audit (2026-10-08)

Every 8-K in the local database (22, filed 2026-09-08 to 2026-10-08) was read as the summarizer
sees it: the items and EX-99 exhibits, trimmed by `form8k_prompt.build_prompt`. Each one's current
summary (the newest row in `summaries`) was checked against that text for wrong claims, the wrong
actor, invented outcomes, and a missing main event. Older summaries of the same filing were read
too where they show a different mistake. The findings became the labels in
[`config/summary_eval.json`](../config/summary_eval.json), which `filings-benchmark` scores
summaries against ([ADR-0008](adr/0008-llm-providers-failover-and-exhibits.md#measuring-summary-quality-added-2026-10-08)).
The quotes are public SEC data.

## Findings

Of the 22 current summaries, 13 were written by Gemini (`gemini-3.5-flash-lite`) and 9 by the local
model (`llama3.2:3b`), all with prompt version 1.

- **The local model's summaries:** 5 of 9 contain a factual error: the wrong actor (Oracle,
  ExxonMobil), a figure given the wrong label or period (Tesla's term loan and commitments,
  PepsiCo's growth rate, AMD's pricing window), and Tesla's misses a whole item (the terminated
  revolver). Another two miss a secondary event the items report (Nike's failed shareholder
  proposals, Adobe's departing division president).
- **Gemini's summaries:** 1 of 13 contains an error (Salesforce's filing date), and 1 has a
  computed total in its headline (Amazon's £4.25 billion), which the grounding check flags.
- **What the grounding check misses:** every error above except the computed totals. All the
  wrong summaries are fluent and grounded; the numbers they use are in the filing, attached to the
  wrong thing.
- **The known slip on Nike's vote** ("all proposals passed", ADR-0010) is not in the current
  summary, but that summary also never says the two shareholder proposals failed.

Scored with the labels, the stored summaries give: Gemini (20 summaries over time) fact recall
98%, 5% with a forbidden claim; llama3.2:3b (11) fact recall 91%, 45% with a forbidden claim.
They cover different filings, so this is not a model comparison; the benchmark below is.

## Filing by filing

"Correct" means no factual error was found; omissions of minor detail are not listed.

| Filing | Items | Current summary | Verdict |
|---|---|---|---|
| UnitedHealth, conference interview (0000731766-26-000205) | 7.01 | Gemini | Correct |
| Adobe, CEO succession (0000796343-26-000144) | 5.02, 7.01 | llama3.2:3b | Omission: Wadhwani stepping down |
| Amazon, Mandia elected director (0001018724-26-000036) | 5.02 | llama3.2:3b | Correct |
| NIKE, annual meeting votes (0000320187-26-000143) | 5.02, 5.07 | llama3.2:3b | Omission: shareholder proposals failed |
| Adobe, Q3 FY2026 results (0000796343-26-000147) | 2.02 | Gemini | Correct |
| Oracle, Q1 FY27 results and dividends (0001193125-26-387905) | 2.02, 8.01 | Gemini | Correct |
| Amazon, sterling notes (0001104659-26-107526) | 8.01 | Gemini | Computed total in headline |
| Oracle, Ellison cancels 10b5-1 plan (0001193125-26-389753) | 8.01 | llama3.2:3b | **Wrong actor** |
| NIKE, Arnault joins board (0000320187-26-000170) | 5.02, 7.01 | Gemini | Correct |
| PepsiCo, Duato elected director (0000077476-26-000042) | 5.02 | Gemini | Correct |
| Salesforce, Investor Day deck (0001108524-26-000210) | 7.01 | Gemini | **Wrong filing date** |
| McDonald's, NEXT strategy targets (0000063908-26-000076) | 7.01 | llama3.2:3b | Correct |
| Visa, litigation escrow deposit (0001403161-26-000121) | 8.01 | Gemini | Correct |
| Costco, Q4 FY2026 results (0000909832-26-000084) | 2.02 | Gemini | Correct |
| ExxonMobil, floating rate notes (0001193125-26-402986) | 8.01 | llama3.2:3b | **Wrong issuer** |
| AMD, World Labs acquisition (0000002488-26-000182) | 3.02 | llama3.2:3b | **Wrong pricing period** |
| Tesla, credit agreements (0001628280-26-063820) | 1.01, 1.02, 2.03 | llama3.2:3b | **Three wrong claims, missing item, computed total** |
| NIKE, Q1 FY27 results and Pace (0000320187-26-000184) | 2.02, 2.05 | Gemini | Correct |
| Tesla, Q3 deliveries (0001628280-26-064366) | 2.02 | llama3.2:3b | Correct |
| Chevron, CFO succession (0000093410-26-000188) | 5.02 | Gemini | Correct |
| Chevron, Hess Midstream divestiture (0000093410-26-000192) | 7.01, 8.01 | llama3.2:3b | Correct |
| PepsiCo, Q3 2026 results (0000077476-26-000050) | 2.02 | llama3.2:3b | **Year-to-date figure given as the quarter's** |

### The errors, with the evidence

**Oracle, 0001193125-26-389753 (llama3.2:3b): wrong actor.** Headline: "Oracle Corp Cancels Larry
Ellison's Plan to Sell Oracle Stock". The filing: "Larry Ellison, Executive Chair of the Board and
Chief Technology Officer ... has cancelled his 10b5-1 Plan to sell Oracle stock." The summary's body
gets it right; the headline, which is what alerts and the feed show, does not. Three of its five key
points describe the filing rather than the event ("The press release is available online from the
SEC or by contacting Oracle's Investor Relations Department"), and it leaves out that Ellison "has no
other plans to sell any of his Oracle stock". The Gemini summary of the same filing is correct.

**Tesla, 0001628280-26-063820 (llama3.2:3b): three wrong claims and a missing item.**
- "Term Loan Facility: $20.0 billion, 18-month delayed draw term loan facility". The filing:
  "$20.0 billion senior unsecured three-year delayed draw term loan facility"; 18 months is the
  draw window ("no more than ten times during the 18-month period following the closing date").
- "Total commitments: up to $14.0 billion across the Revolving Facilities". The revolving
  commitments are $8.0 billion plus $2.0 billion; $14.0 billion needs an optional increase: "Tesla
  may increase the total commitments ... by up to an additional $4.0 billion ... potentially
  increasing the Revolving Facilities to $14.0 billion in the aggregate."
- "$30 billion in credit facilities" is a computed total (the grounding check flags it; the older
  Gemini summary made the same total).
- Item 1.02 is missing: "the Existing Revolving Credit Agreement [$5.0 billion, January 20, 2023]
  was terminated".
- Minor: it lists "Citibank, Wells Fargo Bank, and Wells Fargo Bank" as the counterparties; they
  are the administrative agents.

**PepsiCo, 0000077476-26-000050 (llama3.2:3b): a year-to-date figure given as the quarter's.** Key
point: "Organic revenue growth: 3.1% (2.7% in 12 weeks)". The filing: "Organic revenue increased
3.1% and 2.7%, respectively" for the third quarter and year to date; the quarter is the 12 weeks,
the year to date is 36.

**ExxonMobil, 0001193125-26-402986 (llama3.2:3b): wrong issuer.** Headline: "ExxonMobil Holdings
Corp: Issues $185,883,000 Floating Rate Notes". The filing: "for the issuance and sale by Exxon
Mobil [Exxon Mobil Corporation, the subsidiary] of $185,883,000 aggregate principal amount of its
Floating Rate Notes due 2076, which are to be fully and unconditionally guaranteed by the Company
[ExxonMobil Holdings]". The key points name the issuer correctly.

**AMD, 0000002488-26-000182 (llama3.2:3b): wrong pricing period.** Summary: the share count "will
be calculated based on the daily volume-weighted average price of AMD's common stock over the next
10 trading days". The filing: "over the ten (10) consecutive trading-day period ending on and
including the second trading day immediately preceding the closing date". A key point quotes it
correctly.

**Salesforce, 0001108524-26-000210 (Gemini): wrong filing date.** "posted an investor presentation
to its website and filed it on Form 8-K on September 16, 2026"; key point "Filing Date: September
16, 2026". The 8-K was filed on 2026-09-17 (the prompt's header says so); September 16 is when the
deck was posted and Investor Day held.

**Amazon, 0001104659-26-107526 (Gemini): computed total.** Headline: "Closes Note Offering
Totaling £4.25 Billion". The tranches do add up to £4.25 billion of principal, but the filing
states the "aggregate public offering price of the Notes was £4.242 billion". The grounding check
flags it, as designed (ADR-0009).

### Omissions of a reported event

**NIKE, 0000320187-26-000143 (llama3.2:3b).** Item 5.07 lists six proposals. Two shareholder
proposals were defeated by wide margins (Proposal 5: For 7,363,988, Against 1,073,903,174;
Proposal 6: For 110,862,692, Against 935,451,481). The summary says shareholders "voted on several
other proposals, including the election of directors and ratification of the company's auditor",
without any outcome. Gemini's summary also leaves the failed proposals out. In the ADR-0010
benchmark, both small models said all proposals passed.

**Adobe, 0000796343-26-000144 (llama3.2:3b).** The 5.02 also reports that "David Wadhwani notified
the Company of his decision to step down from his position as President, Creativity & Productivity
Business, effective September 27, 2026"; the summary leaves it out (Gemini's includes it). One key
point turns a quote into a fact: "Chakravarthy will lead the company's growth in an AI-driven era"
is Narayen's opinion ("I could not be more confident that Anil is the right person to lead Adobe's
growth in an AI-driven era").

**Smaller omissions, not errors:** NIKE 0000320187-26-000170 (Gemini) leaves out Arnault's role at
Moët Hennessy; Chevron 0000093410-26-000192 (llama3.2:3b) leaves out the $200 million cash
consideration. Both Oracle 389753 summaries date the cancellation "September 12", the date of the
press release; the filing doesn't say when the plan was cancelled.
