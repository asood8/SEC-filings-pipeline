"""Amended filings (Form 4/A, 8-K/A), as every stage reads them (ADR-0021).

The parser links each amendment to the filing it amends and marks the Form 4 lines it restates
(filings_parser.amendments). This module holds what the summarizer, the notifier, and the API
share: form families, what makes two lines the same trade, whether a Form 4/A changed any
reported line, and the filer's own explanation.
"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Session

from filings_common.db import Filing, Form4Transaction
from filings_common.parsed import Form4

AMENDMENT_SUFFIX = "/A"
# What identifies a transaction line across a filing and its amendment.
LineKey = tuple[date | None, str | None, str | None, str, bool, str | None, str]
# The filer's explanation, quoted in summaries: about two sentences.
MAX_REASON_CHARS = 400


def is_amendment(form_type: str) -> bool:
    return form_type.endswith(AMENDMENT_SUFFIX)


def base_form(form_type: str) -> str:
    """The form an amendment amends ("4/A" -> "4"); any other form is its own."""
    return form_type.removesuffix(AMENDMENT_SUFFIX)


def with_amendments(form_types: Iterable[str]) -> list[str]:
    """The forms and their amendments: choosing Form 4s includes Form 4/As."""
    forms = {base_form(f) for f in form_types}
    return sorted(forms | {f + AMENDMENT_SUFFIX for f in forms})


def line_key(
    transaction_date: date | None,
    code: str | None,
    acquired_disposed: str | None,
    security_title: str,
    derivative: bool,
    direct_or_indirect: str | None,
    nature_of_ownership: str | None,
) -> LineKey:
    """Same date, code, acquired or disposed, security, table, and ownership. Text is compared
    without case or spacing: filers retype "Common Stock" as "COMMON STOCK"."""
    return (
        transaction_date,
        code,
        acquired_disposed,
        _normalized(security_title),
        derivative,
        direct_or_indirect,
        _normalized(nature_of_ownership),
    )


def _normalized(text: str | None) -> str:
    return " ".join((text or "").split()).lower()


def changes_nothing(session: Session, accession_no: str) -> bool:
    """Whether a Form 4/A linked to its original restates the original's lines unchanged (same
    shares, price, and holding after) and adds none: a paperwork amendment, such as one filed
    only to attach a power of attorney. False for an unlinked amendment: there is nothing to
    compare it with."""
    linked = session.scalar(
        sa.select(Filing.amends_accession_no).where(Filing.accession_no == accession_no)
    )
    if linked is None:
        return False
    t = Form4Transaction
    columns = (
        t.transaction_date,
        t.code,
        t.acquired_disposed,
        t.security_title,
        t.is_derivative,
        t.direct_or_indirect,
        t.nature_of_ownership,
        t.shares,
        t.price_per_share,
        t.shares_owned_after,
    )

    def figures(where: sa.ColumnElement[bool]) -> Counter[tuple[object, ...]]:
        return Counter(
            (line_key(*row[:7]), *row[7:])
            for row in session.execute(sa.select(*columns).where(where))
        )

    return figures(t.accession_no == accession_no) == figures(t.superseded_by == accession_no)


# Sentences that say why a Form 4/A was filed: "This amendment is being filed to correct...",
# "The original Form 4 is being amended...". Not "the Amended and Restated 2021 Stock Plan".
_SAYS_AMENDS = re.compile(r"\b(?:amend(?:s|ed|ing|ment)?\b(?! and restated)|form 4/a\b)", re.I)
# Or that name a mistake in an earlier report: "...was inadvertently overstated by 2,829 shares
# in the Reporting Persons' original Form 4", "This filing corrects a reported gift".
_SAYS_MISTAKE = re.compile(
    r"\b(?:inadvertent(?:ly)?|omitted|omission|erroneous(?:ly)?|error|mistaken(?:ly)?|"
    r"incorrect(?:ly)?|correct(?:s|ed|ion|ing)?)\b",
    re.I,
)
_SAYS_EARLIER = re.compile(r"\b(?:original(?:ly)?|previous(?:ly)?|prior|earlier)\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“])")


def amendment_reason(form: Form4) -> str | None:
    """The filer's explanation of a Form 4/A, in its own words: from the first sentence of the
    remarks or footnotes that says it amends, else the first that names a mistake in an earlier
    report, about two sentences. None when no sentence does."""
    texts = [form.remarks or "", *form.footnotes.values()]
    sentences = [_SENTENCE_END.split(" ".join(text.split())) for text in texts]
    for says in (_says_it_amends, _names_a_mistake):
        for text in sentences:
            for i, sentence in enumerate(text):
                if says(sentence):
                    return _clip(text[i:])
    return None


def _says_it_amends(sentence: str) -> bool:
    return _SAYS_AMENDS.search(sentence) is not None


def _names_a_mistake(sentence: str) -> bool:
    return _SAYS_MISTAKE.search(sentence) is not None and _SAYS_EARLIER.search(sentence) is not None


def _clip(sentences: Sequence[str]) -> str:
    """Whole sentences up to MAX_REASON_CHARS; a longer first sentence is cut at a word."""
    kept = sentences[0]
    for sentence in sentences[1:]:
        if len(kept) + 1 + len(sentence) > MAX_REASON_CHARS:
            break
        kept += " " + sentence
    if len(kept) > MAX_REASON_CHARS:
        kept = kept[: MAX_REASON_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return kept
