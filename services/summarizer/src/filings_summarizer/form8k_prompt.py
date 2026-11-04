"""The 8-K summary prompt. Changing it in any way that changes answers means bumping PROMPT_VERSION.

The prompt holds the filing's items and EX-99 exhibits, each trimmed at a line boundary, so it
fits a small local model's context window. Press releases put the headline facts first, so
keeping the beginning keeps what matters.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

from filings_common.parsed import Form8K

PROMPT_VERSION = 1

MAX_ITEM_CHARS = 6_000
MAX_EXHIBIT_CHARS = 8_000
MAX_PROMPT_CHARS = 24_000
TRUNCATED = "[... truncated]"

SYSTEM = """\
You summarize SEC Form 8-K current reports for investors.
Use only facts stated in the filing text provided. Never add outside knowledge, speculate, or give \
investment advice. Keep numbers, names, and dates exactly as written in the filing, and do not \
calculate totals or any other figures the filing does not state.
If an item only refers to an exhibit that is not included, say so plainly instead of guessing \
what it contains.
Return JSON with:
- headline: one line, at most 120 characters, naming the company and the main event;
- summary: 2 to 4 plain-English sentences;
- key_points: 2 to 5 short, concrete facts (names, dates, amounts)."""

# In an 8-K/A's prompt, after the header.
AMENDMENT_TASK = (
    "This filing amends an earlier 8-K, which is not included. In the summary, say what this "
    "amendment adds or changes."
)

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "At most 120 characters."},
        "summary": {"type": "string", "description": "2 to 4 sentences."},
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2 to 5 short facts.",
        },
    },
    "required": ["headline", "summary", "key_points"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Amended:
    """What a summary of an amendment (4/A, 8-K/A) says about it (ADR-0021)."""

    # The amended filing's date: as the amendment gives it, else the linked filing's.
    original_filed_at: date | None
    # A Form 4/A that restates its original's lines unchanged (amendments.changes_nothing).
    changes_nothing: bool = False


@dataclass(frozen=True)
class FilingContext:
    accession_no: str
    form_type: str
    company_name: str
    ticker: str | None
    filed_at: date
    amends: Amended | None = None  # set for amendments only


def truncate(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a line boundary and marked as truncated."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - len(TRUNCATED) - 1)
    if cut <= 0:
        cut = limit - len(TRUNCATED) - 1
    return f"{text[:cut].rstrip()}\n{TRUNCATED}"


def build_prompt(form: Form8K, context: FilingContext) -> str:
    """The filing as the model sees it. An 8-K/A gets a header saying what it amends and asking
    what it adds or changes, and its explanatory note; an 8-K's prompt is the same as before
    amendments were collected, so PROMPT_VERSION and the benchmark's results still hold."""
    company = (
        f"{context.company_name} ({context.ticker})" if context.ticker else context.company_name
    )
    if context.amends is None:
        sections = [f"Company: {company}\nForm 8-K filed {context.filed_at.isoformat()}"]
    else:
        original = context.amends.original_filed_at
        amended = f"the Form 8-K filed {original.isoformat()}" if original else "an earlier 8-K"
        sections = [
            f"Company: {company}\nForm 8-K/A filed {context.filed_at.isoformat()}, an amendment "
            f"to {amended}\n{AMENDMENT_TASK}"
        ]
        if form.explanatory_note:
            sections.append(
                f"## Explanatory note\n{truncate(form.explanatory_note, MAX_ITEM_CHARS)}"
            )
    sections += [
        f"## Item {item.number}: {item.title}\n{truncate(item.text, MAX_ITEM_CHARS)}"
        for item in form.items
    ]
    sections += [
        f"## Exhibit {exhibit.type}"
        + (f": {exhibit.description}" if exhibit.description else "")
        + f"\n{truncate(exhibit.text, MAX_EXHIBIT_CHARS)}"
        for exhibit in form.exhibits
    ]
    return truncate("\n\n".join(sections), MAX_PROMPT_CHARS)
