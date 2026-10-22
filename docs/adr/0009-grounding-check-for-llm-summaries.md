# ADR-0009: Grounding check for LLM summaries

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

On the first live run of 8-K summaries (ADR-0008), the small Gemini model reported totals that the
filings never state, even though the prompt forbids computed figures:

- "£4.25 billion" of Amazon notes (four tranches added up);
- "$30.0 billion" of Tesla credit facilities.

The arithmetic was right both times, but a filing summary should only state the filing's own
figures. Prompt wording alone doesn't reliably stop a small model doing this.

## Decision

Every LLM summary goes through a deterministic **grounding check** against exactly the prompt the
model was given, header and trimming included. The check lists any number in the summary whose
value appears nowhere in that prompt.

- **Values, not spellings:**
  - "£1.25 billion" matches "£1,250,000,000", and "5.2%" matches "5.200%";
  - "3" matches "three";
  - in a table "in millions", a bare "6,000" also counts as 6 billion.
- **Rounding is allowed** to the precision the summary states: "$1.3 billion" matches
  "$1,324 million", but "$1.4 billion" does not.
- **Some tokens are skipped:** numbers glued to letters ("Q3", "FY27").
- **Results are stored with the summary** in `summaries.ungrounded_numbers`, and flagged numbers are
  logged as a warning. `[]` means everything was found. `NULL` means the summary wasn't checked;
  template summaries compute totals and averages on purpose, so they aren't checked.
- **Flag, don't reject.** A flagged number is one a reader should verify, not necessarily a wrong
  one. The summary is still stored and announced, and the flag is available to show a caution
  marker in the API and frontend.

### Names (added 2026-10-22)

The same check for names: a capitalized word in an 8-K summary that the text the model was given
never uses, such as a person the model invented, is stored in `summaries.ungrounded_names`
(migration 0485ad731202), returned by the API, and shown on the filing page ("Check these names
against the original"). What counts as a name, so ordinary words don't fire it: capitalized, at
least three letters, not starting a sentence or key point, not all capitals (CEO, NVDA), not a
month or weekday, and compared by a crude stem ("Elects" matches "elected", "Ellison's"
matches "Ellison"). A Title Case headline is skipped, since all its words are capitalized.

Measured on the 22 local 8-K summaries before it was enabled: a first version that took
capitalized phrases flagged 13 (mostly Title Case headlines and verbs like "Elects"); this one
flags none. Like the number check, it can't catch a real name in the wrong role: the labeled
errors in config/summary_eval.json (Oracle's actor, Chevron's roles) use names the filing has.
Summaries written before it have NULL, "not checked".

## Alternatives considered

- **Rejecting flagged summaries** (fail over, or retry). With only one provider available, retries
  would loop on the same habit and end in the dead-letter topic, which is worse than a flagged but
  otherwise correct summary.
- **A second LLM pass to verify facts.** Slower, costs quota, and is itself unverifiable. A
  deterministic check is cheap and gives the same answer every time.
- **Checking names and dates as well as numbers.** Numbers are where small models drift and where
  errors matter most. Entity checks would be much fuzzier.

## Consequences

- **Live result:** on the 20 real 8-K summaries, the check flagged exactly the two computed totals
  that a manual review had found, and passed the other 18. That includes reformatted figures
  ("£1.25 billion"), rounded figures, and the filing date taken from the prompt header.
- **Measuring models:** the flag rate becomes a quality measure for comparing models and prompt
  versions in the Phase 8 benchmark, and a metric in Phase 6.
- **Known limits:** derived numbers that happen to equal some other number in the source pass
  unnoticed. So do wrong numbers that look like rounding of a nearby source figure.
