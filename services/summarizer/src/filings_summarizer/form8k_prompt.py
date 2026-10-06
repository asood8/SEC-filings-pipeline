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
class FilingContext:
    accession_no: str
    form_type: str
    company_name: str
    ticker: str | None
    filed_at: date


def truncate(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a line boundary and marked as truncated."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - len(TRUNCATED) - 1)
    if cut <= 0:
        cut = limit - len(TRUNCATED) - 1
    return f"{text[:cut].rstrip()}\n{TRUNCATED}"


def build_prompt(form: Form8K, context: FilingContext) -> str:
    company = (
        f"{context.company_name} ({context.ticker})" if context.ticker else context.company_name
    )
    sections = [f"Company: {company}\nForm 8-K filed {context.filed_at.isoformat()}"]
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
