"""Grounding check: which numbers in an LLM summary don't appear in the text the model was given?

Small models sometimes compute figures the filing never states (adding up note tranches into a
"total", for example). This check catches them deterministically. It compares numeric *values*,
not spellings, so these all count as found:
- "£1.25 billion" against "£1,250,000,000";
- "5.2%" against "5.200%";
- "$1.3 billion" against "$1,324 million": a summary number may round from its stated precision;
- "$6.0 billion" against a table entry "6,000" when the source says "in millions";
- "3 directors" against "three directors";
- "$6.80 to $6.85 billion" against "$6.80 billion to $6.85 billion" (a range's unit covers both).

Numbers glued to letters ("Q3", "FY27") are ignored. A flagged number is not necessarily wrong;
it is one a reader should verify.
"""

import bisect
import re
from dataclasses import dataclass

_SCALES = {
    "thousand": 1e3,
    "k": 1e3,
    "million": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "trillion": 1e12,
}
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?"
    r"(?:\s?(thousand|million|billion|trillion|bn|mn|[kmb])\b)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# Between the two ends of a range: "$6.80 to $6.85 billion", "$1.93-1.95 billion". "And" only
# makes a range after "between" ("between 5 and 10 million"), not in "2029 and £1.0 billion".
_RANGE_JOINER = re.compile(r"^\s*(?:to|through|-|\u2013|\u2014)\s*[$£€]?\s*$", re.IGNORECASE)
_AND_JOINER = re.compile(r"^\s*and\s*[$£€]?\s*$", re.IGNORECASE)
_BETWEEN = re.compile(r"\bbetween\s*[$£€]?\s*$", re.IGNORECASE)
# "(in millions)", "in thousands, except per share data" ...
_TABLE_UNITS = re.compile(r"\bin (thousands|millions|billions)\b", re.IGNORECASE)
_WORDS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
]
_ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth"
_SPELLED = {w: float(i) for i, w in enumerate(_WORDS)} | {
    w: float(i) for i, w in enumerate(_ORDINALS.split(), start=1)
}
_SPELLED_PATTERN = re.compile(r"\b(" + "|".join(_SPELLED) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Number:
    text: str
    value: float
    tolerance: float  # half a unit of the last stated digit, in the same scale
    has_unit: bool  # written with "million", "bn", ...


def _numbers(text: str) -> list[_Number]:
    matches = list(_NUMBER.finditer(text))
    found = []
    for i, match in enumerate(matches):
        whole, fraction, unit = match.group(1), match.group(2) or "", match.group(3)
        if not unit and i + 1 < len(matches):
            following = matches[i + 1]
            between = text[match.end() : following.start()]
            is_range = _RANGE_JOINER.match(between) or (
                _AND_JOINER.match(between) and _BETWEEN.search(text[: match.start()])
            )
            if following.group(3) and is_range:
                unit = following.group(3)  # the unit after a range covers both ends
        scale = _SCALES[unit.lower()] if unit else 1.0
        decimals = len(fraction) - 1 if fraction else 0
        value = float(whole.replace(",", "") + fraction) * scale
        tolerance = 0.5 * 10.0**-decimals * scale
        found.append(_Number(match.group(0), value, tolerance, has_unit=bool(unit)))
    return found


def _source_values(source: str) -> list[float]:
    values = {n.value for n in _numbers(source)}
    values |= {_SPELLED[w.lower()] for w in _SPELLED_PATTERN.findall(source)}
    # Tables "in millions" state 6,000 for $6 billion: also accept bare numbers scaled up.
    bare = [n.value for n in _numbers(source) if not n.has_unit]
    for unit in {u.lower() for u in _TABLE_UNITS.findall(source)}:
        factor = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}[unit]
        values |= {value * factor for value in bare}
    return sorted(values)


def ungrounded_numbers(summary_text: str, source: str) -> list[str]:
    """Numbers in `summary_text` whose value appears nowhere in `source`, in order of appearance."""
    values = _source_values(source)
    flagged = []
    for number in _numbers(summary_text):
        # Allow float noise on top of the rounding tolerance.
        slack = number.tolerance + abs(number.value) * 1e-9
        i = bisect.bisect_left(values, number.value - slack)
        if i == len(values) or values[i] > number.value + slack:
            flagged.append(number.text)
    # One entry per figure, however it was capitalized ("Billion", "billion"), first seen first.
    unique: dict[str, str] = {}
    for text in flagged:
        unique.setdefault(text.lower(), text)
    return list(unique.values())
