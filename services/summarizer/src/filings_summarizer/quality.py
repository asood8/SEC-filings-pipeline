"""Summary quality on a labeled set: does an 8-K summary state what matters, and does it avoid
claims the filing contradicts?

The grounding check only covers numbers, so a summary can be well formed, fully grounded, and
still wrong: "all proposals passed" when a shareholder proposal failed, or "Oracle cancels
Ellison's plan" when Ellison cancelled it himself. This check uses labels written by reading each
filing (`config/summary_eval.json`):

- **Facts** a correct summary must state: the event, who did it, the main figure. A fact is a list
  of regular expressions (any one matching is enough) and/or a list of figures (any one stated is
  enough, read with the grounding check's normalization, so "16 million" states 16,000,000). When
  a fact has both, both must hold.
- **Forbidden claims**: statements the filing contradicts, such as an outcome it doesn't report or
  the wrong actor. Any of a claim's regular expressions matching flags it.

Patterns match case-insensitively against the headline, the summary, and each key point
separately (never across two of them), after typographic quotes, dashes, and spaces are made plain.
No model judges another: every check is a regular expression or a number comparison, so each score
can be traced to the label that produced it. The labels are only as good as their patterns; a
correct summary in words nobody anticipated scores as a miss, so read the misses before trusting a
small difference.
"""

import functools
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from filings_common.summaries import FilingSummary
from filings_summarizer.grounding import figure_value, states_value

DEFAULT_LABELS = Path("config/summary_eval.json")

AccessionNo = Annotated[str, StringConstraints(pattern=r"^\d{10}-\d{2}-\d{6}$")]
Text = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]

# Typographic characters that models and filings use interchangeably with plain ones: curly
# quotes, Unicode hyphens and dashes, no-break spaces.
_PLAIN = str.maketrans(
    dict.fromkeys(map(chr, (0x2018, 0x2019)), "'")
    | dict.fromkeys(map(chr, (0x201C, 0x201D)), '"')
    | dict.fromkeys(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014)), "-")
    | dict.fromkeys(map(chr, (0x00A0, 0x202F)), " ")
)


class LabelError(ValueError):
    """The label file is missing, isn't JSON, or doesn't have the expected shape."""


def _compile_all(patterns: tuple[str, ...]) -> tuple[str, ...]:
    for pattern in patterns:
        try:
            _compiled(pattern)
        except re.error as exc:
            raise ValueError(f"bad regular expression {pattern!r}: {exc}") from exc
    return patterns


class Fact(BaseModel):
    """Something a correct summary of the filing states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact: Text
    patterns: tuple[Text, ...] = ()
    numbers: tuple[Text, ...] = ()

    @field_validator("patterns")
    @classmethod
    def _patterns_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _compile_all(value)

    @field_validator("numbers")
    @classmethod
    def _numbers_parse(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for figure in value:
            figure_value(figure)  # raises ValueError unless it is one number
        return value

    @model_validator(mode="after")
    def _checkable(self) -> Self:
        if not self.patterns and not self.numbers:
            raise ValueError(f"fact {self.fact!r} needs patterns or numbers")
        return self


class ForbiddenClaim(BaseModel):
    """A statement the filing contradicts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: Text
    patterns: tuple[Text, ...] = Field(min_length=1)
    # What the filing says instead, quoted, so the label can be checked.
    evidence: str = ""

    @field_validator("patterns")
    @classmethod
    def _patterns_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _compile_all(value)


class FilingLabels(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accession_no: AccessionNo
    company: Text
    # What the 8-K reports, and how the labels were chosen.
    note: str = ""
    facts: tuple[Fact, ...] = Field(min_length=1)
    forbidden: tuple[ForbiddenClaim, ...] = ()


class LabelFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    filings: list[FilingLabels] = Field(min_length=1)

    @field_validator("filings")
    @classmethod
    def _unique_filings(cls, value: list[FilingLabels]) -> list[FilingLabels]:
        seen: set[str] = set()
        for labels in value:
            if labels.accession_no in seen:
                raise ValueError(f"{labels.accession_no} is labeled twice")
            seen.add(labels.accession_no)
        return value


def load_labels(path: Path) -> dict[str, FilingLabels]:
    """Labels by accession number."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabelError(f"can't read {path}: {exc.strerror or exc}") from exc
    try:
        filings = LabelFile.model_validate_json(text).filings
    except ValidationError as exc:
        raise LabelError(f"{path} is not a valid label file:\n{exc}") from exc
    return {labels.accession_no: labels for labels in filings}


@functools.cache
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


@functools.cache
def _value(figure: str) -> float:
    return figure_value(figure)


def plain(text: str) -> str:
    """Typographic quotes, dashes, and spaces made plain, runs of whitespace collapsed."""
    return " ".join(text.translate(_PLAIN).split())


def segments(summary: FilingSummary) -> list[str]:
    """The parts patterns are matched against, one at a time."""
    return [plain(part) for part in (summary.headline, summary.summary, *summary.key_points)]


def _matches(patterns: Iterable[str], parts: list[str]) -> bool:
    return any(_compiled(p).search(part) for p in patterns for part in parts)


def states(fact: Fact, parts: list[str]) -> bool:
    if fact.patterns and not _matches(fact.patterns, parts):
        return False
    if fact.numbers:
        return any(states_value(part, _value(f)) for f in fact.numbers for part in parts)
    return True


@dataclass(frozen=True)
class QualityScore:
    """One summary against its filing's labels."""

    facts: int
    stated: int
    fact_recall: float
    missing: list[str]  # the facts not stated
    forbidden: list[str]  # the forbidden claims made


def score_summary(summary: FilingSummary, labels: FilingLabels) -> QualityScore:
    parts = segments(summary)
    missing = [fact.fact for fact in labels.facts if not states(fact, parts)]
    stated = len(labels.facts) - len(missing)
    return QualityScore(
        facts=len(labels.facts),
        stated=stated,
        fact_recall=round(stated / len(labels.facts), 4),
        missing=missing,
        forbidden=[c.claim for c in labels.forbidden if _matches(c.patterns, parts)],
    )


def quality_stats(scores: Iterable[QualityScore]) -> dict[str, Any]:
    """Averages over scored summaries; each summary counts the same, whatever its fact count."""
    scored = list(scores)
    if not scored:
        return {
            "scored": 0,
            "fact_recall": None,
            "forbidden_share": None,
            "forbidden_claims": None,
        }
    return {
        "scored": len(scored),
        "fact_recall": round(statistics.mean(s.fact_recall for s in scored), 2),
        "forbidden_share": round(sum(bool(s.forbidden) for s in scored) / len(scored), 2),
        "forbidden_claims": sum(len(s.forbidden) for s in scored),
    }
