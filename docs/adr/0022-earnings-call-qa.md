# ADR-0022: Earnings-call Q&A summaries from companies' own transcripts

- **Status:** Accepted (2026-11-04)
- **Date:** 2026-11-04

## Context

An earnings call's Q&A is where analysts press management, and none of it is in EDGAR: the 8-K
only announces the call, which the pipeline already reads (ADR-0007, "Investor calls"). The
brief's open decision (§12) asked for a proposal and a laptop pilot before building anything: the
call's content would mean fetching it from each company's investor site, maybe a speech-to-text
model (a new component), summaries in parts, and each site's terms.

**The user's decisions (2026-11-04):**

- Publish only our own short summaries and a link to the company's page, never a full transcript
  or the audio. A full transcript can be considered later.
- Avoid any site whose terms deny automated downloads.
- Skip webcasts behind a sign-up form (no browser automation).
- whisper.cpp may be added if it is needed.

### The survey

For each of the 35 watchlist companies' latest calls (2026-11-04): the earnings 8-K's press
release (via the EDGAR client), the company's investor pages, its robots.txt, and the terms of
use its site links to.

| Ticker | Own transcript | Audio | Terms on automated access / downloads | Verdict |
|---|---|---|---|---|
| MSFT | yes, on the call's page | webcast | scraping barred for AI services only; documents for non-commercial use; no sound may be copied | transcript |
| GOOGL | yes, PDF | YouTube | automated access barred only against robots.txt, which allows it | transcript |
| JPM | yes, PDF | webcast, phone | silent on automation; copying only as fair use allows | transcript |
| BAC | yes, PDF | webcast, phone | silent | transcript |
| JNJ | yes, PDF (LSEG) | webcast, podcast | silent; download for personal, non-commercial use | transcript |
| PEP | yes, PDF (FactSet) | webcast | silent; download for personal, non-commercial use | transcript |
| NKE | yes, PDF and HTML | archived audio | the investor site's terms are silent | transcript |
| V | yes, PDF (FactSet) | webcast | silent; download for personal, non-commercial use | not in v1 (below) |
| AAPL, AMZN, NVDA, ORCL, ADBE, INTC, GS, UNH, WMT, COST, KO, DIS, BA, META | some (AMZN and KO post audio files with no sign-up; KO, META and NVDA post transcripts) | | ban robots, spiders, scrapers, data mining or automated means; Costco also bans downloading its webcasts | avoid |
| TSLA, AVGO, LLY, PFE, MCD, CVX, MA | | | the site refuses automated requests (HTTP 403) | avoid |
| CRM, XOM | | | use or copying only with permission, or printing one copy | avoid |
| AMD, CAT, NFLX | | | terms unreadable or not found | avoid (unverified) |
| BRK-B | no earnings call | | | n/a |

So **8 companies are usable, every one through its own transcript, and none needs
speech-to-text**: the only audio that downloads without a sign-up (Amazon's MP3s, Coca-Cola's)
belongs to sites whose terms ban robots. robots.txt allowed a generic crawler on every site that
answered. Replays stay up from two weeks (Apple) to a year (Microsoft, Tesla, Broadcom), and a
transcript appears the same day (Microsoft, Bank of America) or weeks later (PepsiCo's Q3 one was
still missing 27 days after the call).

Visa is left out of v1: its transcript list is reachable only through its investor site's data
feed, called with a key embedded in the page, not from a plain page or a predictable address.

### The second survey (2026-11-06): the 64 companies added to the watchlist

The same checks for the Fortune 100 companies added that day, with the project's own site client
(robots.txt honored, a second between requests to a host, the `Filingwire/1.0` User-Agent): each
investor site's home page, the terms of use it links to (or the company site's), and its results
pages for transcripts.

| Verdict | Companies |
|---|---|
| **transcript** (3) | **F**: own PDFs; the terms let crawlers in if they follow robots.txt, downloads for personal, non-commercial use. **DELL**: own PDFs; downloads for personal, non-commercial use, no mirroring. **SNX**: own PDFs; the only terms the site links are its sales terms, silent on the site. |
| the site refuses automated requests (HTTP 403) (17) | GM, MPC, SNEX, HUM, CMCSA, RTX, ET, LMT, COF, ALL, GLXY, ABBV, TJX, UAL, CHTR, AAL, EPD |
| terms ban robots, scrapers, data mining or automated access (16) | CVS, MCK, C, FNMA, FMCC, VZ, T, VLO, TGT, PG, HCA, MRK, PRU, CSCO, TSN, TRV |
| terms bar downloading, copying or storing the content (7) | COR, KR ("insignificant portions"), PSX, COP, FDX, HPQ, MET ("substantially copy on your own server") |
| terms allow it, but no transcripts of their own (5) | ELV, WFC, MS, PGR, BMY |
| terms unreadable: their host refuses automated requests, the page is built by scripts, or it wasn't found (16) | HD, LOW, DAL (all three post transcripts), CNC (robots.txt disallows its terms), UPS, SYY, AXP, ACI, ADM, IBM, PFGC, INGM, GD, UBER, CI, CAH |

So **3 of 64 are usable**, again all through their own transcripts. Home Depot, Lowe's and Delta
publish transcripts, but their terms couldn't be read here; reading them in a browser could add
them. Ford's newest transcript can first appear under a name without its year
(`content_files/Ford-Q3-Earnings-Transcript.pdf`), which can't be dated safely, so its source reads
only the dated names under `doc_financials/`.

Two changes came with it: listing pages are also read for links in the JSON some sites embed for
their document lists (Ford's), and an analyst's firm may be written with initials ("J.P. Morgan"
was cut to "J"; `SPLITTER_VERSION` 2).

### The pilot (laptop, llama3.2:3b)

Seven real transcripts (Microsoft, Alphabet, JPMorgan, Bank of America, J&J, PepsiCo, Nike):

- **Splitting needs no model.** Every transcript labels the operator's turns, and the operator
  introduces each analyst, in one of a few ways ("Our next question comes from the line of Erika
  Najarian with UBS", "We'll move next to Manan Gosalia with Morgan Stanley", J&J's bare "Asad
  Haider, Goldman Sachs."). Rules on those split all seven into 7 to 15 exchanges each, 200 to
  1,300 words long, which fits the model's context with room to spare.
- **Summaries, per exchange:** 27 exchanges from three calls (JPMorgan, Nike, Alphabet), median
  35 s each (max 58 s), and 30 to 90 s for a call's takeaways. No summary stated a number or a
  name the transcript doesn't have (the existing grounding checks).
- **Quality** on 6 exchanges labeled by hand (facts a correct summary states, claims it must not
  make, scored by the 8-K benchmark's matcher): **69% of the facts, no forbidden claim**, once the
  text is cleaned (FactSet's dot leaders alone were a quarter of JPMorgan's words; on the raw
  text, 60%). Reading them: the model often covers only the first of an analyst's two or three
  questions (Alphabet's 103% cloud growth went missing that way), and quotes management rather
  than summarizing.
- **A prompt asking it to cover every question was worse**: 57%. Without a length cap one answer
  ran on for 4,300 tokens until the 10-minute timeout; with a 400-token cap, 2 of 6 answers ran
  past it, and the rest were longer and said things nobody said (Nike's "gross margin expected to
  be down year-over-year", which was SG&A; an analyst's premise about Alphabet's capital spending
  put in the CFO's mouth). The first prompt is kept, with answers capped: the same lesson as the
  8-K prompts (ADR-0010), that asking a small model for more makes it invent.
- **No speech-to-text was measured.** No usable company needs it, and the only audio found with
  a matching transcript and no sign-up was barred by its site's terms; the Federal Reserve's
  press conferences (public domain) sit behind a video player.

## Decision

1. **Companies' own transcripts only, from 7 companies** (Microsoft, Alphabet, JPMorgan, Bank of
   America, J&J, PepsiCo, Nike), listed in `config/transcript_sources.toml`. A source is either a
   listing page and a pattern for its transcript links, or address templates filled from the
   date of each earnings 8-K (item 2.02) discovery already stored, so nothing here calls SEC.
2. **Polite fetching**: a User-Agent that names the project (and no contact address, which is
   for SEC), robots.txt honored for every request and redirect, at least a second between
   requests to one site, documents capped at 4 MB, only calls of the last 120 days. A site that
   starts refusing is dropped, not worked around.
3. **No speech-to-text: whisper.cpp is not added.** It would serve no company. If a company
   whose terms allow it ever posts audio but no transcript, revisit this with a measurement on
   the A1 (third-party numbers on an 8-core ARM board put whisper.cpp's small model at about
   twice real time).
4. **Stored, never served.** The text goes to MongoDB (`call_transcripts`, keyed by the source
   URL), a row to Postgres (`call_transcripts`: company, call date, source URL, size). The API and
   website show only our summaries and the link. Test fixtures are written for the tests in each
   real format; no real transcript is committed (FactSet's and LSEG's notices bar republishing).
5. **Splitting by rules** (`filings_common.transcripts`): exchanges start at the operator's
   introductions; the transcript's own labels say who answers.
6. **Summaries in two steps**: each exchange (who asked, what about, what management said),
   then the call's takeaways from those, with answers capped (400 and 600 tokens). The
   grounding checks run on each against the transcript. History is kept per prompt version, as
   for filings. The labeled exchanges become a benchmark, like the 8-Ks'.
7. **A nightly job** (`call-transcripts` CronJob, 03:40 UTC, in the discovery image), and an alert
   when a company's earnings 8-K is three weeks old without a transcript (a source broke).

Built in three PRs: fetching and storing; summaries and their benchmark; the API and the
website. Alerts came after (ADR-0012, "Earnings-call Q&A alerts"): a call's first summary alerts
the company's subscriptions to every form type or to call Q&A.

### First real run (added 2026-11-04)

On the laptop, against the real sites and the local model only (no Gemini):

- **Fetching**: all 7 sources worked and stored 10 transcripts (the four date-based ones by their
  October calls' dates); a rerun stored nothing. It found PepsiCo's links on a CloudFront host
  whose robots.txt redirects (now followed), and flagged PepsiCo as overdue: its Q3 transcript
  was still not posted 27 days after the call.
- **Summaries**: 9 of 10 calls, 88 exchanges, in 84 minutes (about 40 s an exchange), at most
  114 MB of memory. 3 exchanges failed and are listed as failed. JPMorgan's call (12
  exchanges) failed as a whole: its takeaways didn't fit the summary shape, and once fitted,
  the model wrote key point after key point until the 700-token cap cut the answer off. So the
  response schemas now bound what the model writes (each field's length, 5 key points), which
  both providers enforce while decoding; the takeaways' cap is 1,000 tokens, and what comes back
  is still fitted (a headline cut at 200 characters, blank key points dropped) rather than
  refused (prompt version 2).
- **Before that**, 4 of Microsoft's 7 exchanges had failed over to Gemini. The log didn't say
  why (it now names the failure's type); the likely cause is the model copying long multi-part
  questions nearly word for word, past the 600-character limit. Long text is now cut at a word
  instead, and the same call then went through locally, 7 of 7.
- **What the checks caught**: one exchange summary made up a closing sentence for Satya Nadella
  ("we're looking at a 10% increase in margins over the next few years"). Its own grounding check
  flagged "10", but the takeaways repeated the claim and passed, because "10" appears elsewhere
  in the call. A figure or name flagged in an exchange is now flagged in the takeaways too.
- **What they can't catch**: the takeaways credited Amy Hood's answer on financing to Satya
  Nadella. The exchange summaries often don't say who answered, and the takeaways guess.

## Alternatives considered

- **Webcast audio and whisper.cpp.** Needed for no usable company (above).
- **Gemini with the audio.** No new component, but the same audio problem, and a one-hour call
  is about 115,000 input tokens on a free tier whose current limits couldn't be confirmed.
- **Third-party transcript sites** (Seeking Alpha and others): not the companies' own words as
  published, often paywalled, and another set of terms per site.
- **An investor site's data feed** (Visa's and others on the same platform): reached with a key
  taken from the page, not an interface the site offers. Not used.
- **A browser for sign-up forms and players.** The user ruled it out; it would also be another
  heavy component.
- **Speaker identification models** (pyannote): need PyTorch, too heavy for the VM, and the
  operator's introductions already say who asks.

## Consequences

- Coverage is 10 of the 99 watchlist companies (10%): 7 of the first 35, and 3 of the 64 added on
  2026-11-06 (Ford, Dell, TD SYNNEX). Adding one means reading its terms first (runbook,
  "Earnings-call transcripts").
- Sources are per-site and will break when a site changes; the overdue alert says when.
- The small model's known weakness (who did what) matters more in Q&A, which is mostly who said
  what. The labels make that measurable.
- The brief's open decision on earnings-call Q&A is closed.
