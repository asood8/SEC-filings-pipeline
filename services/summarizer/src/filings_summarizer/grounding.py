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

The same idea applies to names (`ungrounded_names`): a capitalized word in the summary that the
source never uses, such as a person the model invented, is flagged. See that function for what
counts; it can't catch a real name in the wrong role ("Oracle cancels Ellison's plan" when
Ellison cancelled it).
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


def figure_value(figure: str) -> float:
    """The value of one written figure ("£4.242 billion", "16,000,000", "5.6%").

    Raises ValueError unless `figure` holds exactly one number.
    """
    numbers = _numbers(figure)
    if len(numbers) != 1:
        raise ValueError(f"{figure!r} must hold exactly one number, not {len(numbers)}")
    return numbers[0].value


def states_value(text: str, value: float) -> bool:
    """Whether `text` states `value`, read the way the grounding check reads a summary: in any
    spelling ("16 million" for 16,000,000, "twelve" for 12), and rounded from the precision the
    text states ("$1.3 billion" states 1,324,000,000)."""
    for number in _numbers(text):
        if abs(number.value - value) <= number.tolerance + abs(value) * 1e-9:
            return True
    return any(_SPELLED[word.lower()] == value for word in _SPELLED_PATTERN.findall(text))


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


_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019]*")
# Sentences, and the summary's separate parts: a key point starts its own sentence.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?:;])\s+|\n")
_CALENDAR_WORDS = frozenset(
    [
        *("january", "february", "march", "april", "may", "june", "july", "august"),
        *("september", "october", "november", "december"),
        *("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
    ]
)


def _stem(word: str) -> str:
    """A crude stem, so "Elects" matches "elected" and "Ellison's" matches "Ellison"."""
    word = re.sub(r"['\u2019]s$", "", word.lower())
    for suffix in ("ing", "ed", "es", "s", "d"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _title_case(text: str) -> bool:
    words = re.findall(r"[A-Za-z]{4,}", text)
    return bool(words) and sum(w[0].isupper() for w in words) > len(words) / 2


def ungrounded_names(headline: str, summary: str, key_points: list[str], source: str) -> list[str]:
    """Capitalized words in the summary that `source` never uses, in order of appearance.

    Only words that look like names count: capitalized, at least three letters, not starting a
    sentence or key point, not all capitals (CEO, NVDA), and not a month or weekday. A headline in
    Title Case is skipped, since every word in it is capitalized; one in sentence case is
    checked (a short headline that is mostly names reads as Title Case, but the summary repeats
    its names). Words are compared by a crude stem, so inflections don't count as new names. On the
    22 local 8-K summaries this flagged nothing (2026-10-22).
    """
    stems = {_stem(word) for word in _WORD.findall(source)}
    parts = ([] if _title_case(headline) else [headline]) + [summary, *key_points]
    flagged: dict[str, str] = {}
    for sentence in _SENTENCE_BREAK.split("\n".join(parts)):
        for i, word in enumerate(_WORD.findall(sentence)):
            if i == 0 or not word[0].isupper() or len(word) < 3 or word.isupper():
                continue
            if word.lower() in _CALENDAR_WORDS or _stem(word) in stems:
                continue
            flagged.setdefault(word.lower(), word)
    return list(flagged.values())
