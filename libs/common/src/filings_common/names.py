"""Readable names from EDGAR's: companies, and the people and entities that file Form 4s.

EDGAR's conformed names are often in capitals with a state tag ("BANK OF AMERICA CORP /DE/"), and
list people last name first ("COOK TIMOTHY D"). The Form 4 template, the search index, and the API
all show names through these two functions, so a name reads the same everywhere:

- `company_name("CHEVRON CORP")` is "Chevron Corp".
- `display_name("COOK TIMOTHY D")` is "Timothy D Cook"; an entity keeps its word order:
  "BERKSHIRE HATHAWAY INC" is "Berkshire Hathaway Inc".

A name typed in mixed case keeps its spelling ("McLaughlin Edward Grunde" is "Edward Grunde
McLaughlin"), and so does a mixed-case word in a name in capitals ("ELI LILLY & Co" is "Eli Lilly &
Co"). A few brands have their own spelling ("JPMORGAN CHASE & CO" is "JPMorgan Chase & Co"), and
"COM" joins the word before it ("AMAZON COM INC" is "Amazon.com Inc"). `company_name` is
idempotent: `company_name(company_name(x)) == company_name(x)`, so callers may pass EDGAR's name
or one already shown.

Reordering a person's name is a heuristic: EDGAR doesn't mark where a last name ends. A particle
starts a longer one ("Di Sibio Carmine" is Carmine Di Sibio), but a two-word last name without one
comes out wrong ("MONTGOMERY RICE VALERIE MD", Valerie Montgomery Rice, is shown as "Rice Valerie
Montgomery MD"), and so does a name filed first name first ("SACHIN J. MEHRA" is shown as "J. Mehra
Sachin"). That is why the API also returns names as filed.

A reporting owner's relationship to the company reads the same way: `relationship_label` is
"CEO", "Director", "10% owner", or several of them ("President and CEO, Director"), with officer
titles shortened by `officer_title` ("Chief Financial Officer" is "CFO").
"""

import re
from collections.abc import Mapping, Sequence

from filings_common.parsed import OwnerRelationship

# Words that mark a reporting owner as an entity rather than a person, compared in capitals
# without dots or commas at either end ("L.P." is "L.P").
_ENTITY_WORDS = frozenset(
    [
        "&",
        "INC",
        "INCORPORATED",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LLC",
        "L.L.C",
        "LP",
        "L.P",
        "LLP",
        "LTD",
        "LIMITED",
        "PLC",
        "S.A",
        "N.V",
        "GMBH",
        "TRUST",
        "ESTATE",
        "FUND",
        "FUNDS",
        "HOLDINGS",
        "PARTNERS",
        "PARTNERSHIP",
        "GROUP",
        "BANK",
        "BANC",
        "CAPITAL",
        "MANAGEMENT",
        "ADVISORS",
        "ASSOCIATES",
        "INVESTMENTS",
        "INVESTORS",
        "SECURITIES",
        "VENTURES",
        "FOUNDATION",
        "N.A",
    ]
)
# Generational suffixes and credentials, which stay after the last name ("Roger W. Ferguson Jr."),
# keyed in capitals without dots, with how a name in capitals shows them.
_SUFFIXES = {
    "JR": "Jr",
    "SR": "Sr",
    "II": "II",
    "III": "III",
    "IV": "IV",
    "MD": "MD",
    "PHD": "PhD",
    "ESQ": "Esq",
    "CPA": "CPA",
}
# Particles that begin a last name of several words: "Di Sibio Carmine", "VAN DER BERG JOHN".
# Le, La, and Du only follow another particle ("DE LA CRUZ"): on their own they are last names
# (Vietnamese Le, Chinese Du).
_FIRST_PARTICLES = frozenset({"DA", "DAS", "DE", "DEL", "DELLA", "DI", "DOS", "ST", "VAN", "VON"})
_NEXT_PARTICLES = _FIRST_PARTICLES | {"DEN", "DER", "LA", "LAS", "LE", "LOS"}
_SMALL_WORDS = frozenset({"of", "and", "the", "for", "&"})
# Words that stay in capitals in a name in capitals: Roman numerals, and legal forms and
# abbreviations written that way ("BLACKSTONE FUND III LP", "SAP SE", "US FOODS HOLDING CORP").
_UPPER_WORDS = frozenset(
    {"II", "III", "IV", "LP", "LLC", "LLP", "AG", "SE", "NV", "SA", "US", "USA", "REIT", "ETF"}
)
# A word of letters around an ampersand: "AT&T", "S&P", "H&R".
_AMPERSAND_WORD = re.compile(r"[A-Z]+&[A-Z]+")
# Words with a spelling of their own, which title-casing gets wrong ("JPMORGAN" is JPMorgan, not
# Jpmorgan), keyed as EDGAR writes them in capitals: well-known brands among large US companies.
_SPELLINGS = {
    "ABBVIE": "AbbVie",
    "BLACKROCK": "BlackRock",
    "EXXONMOBIL": "ExxonMobil",
    "FEDEX": "FedEx",
    "JPMORGAN": "JPMorgan",
    "MCDONALDS": "McDonald's",
    "NVIDIA": "NVIDIA",
    "PAYPAL": "PayPal",
    "PEPSICO": "PepsiCo",
    "SAP": "SAP",
    "UNITEDHEALTH": "UnitedHealth",
}
# Officer titles shown as the abbreviations people know them by, and what they stand for. The
# search index treats each pair as synonyms (filings_common.search.ABBREVIATIONS starts with
# these), so "CFO" finds "Chief Financial Officer" and the other way round. Only unambiguous ones:
# CIO can be an information or an investment officer, and CPO a product or a people officer.
OFFICER_ABBREVIATIONS: Mapping[str, str] = {
    "CEO": "chief executive officer",
    "CFO": "chief financial officer",
    "COO": "chief operating officer",
    "CTO": "chief technology officer",
    "CAO": "chief accounting officer",
    "EVP": "executive vice president",
    "SVP": "senior vice president",
    "VP": "vice president",
}
# Each long form as whole words, in any case and spacing; the longest first, so "Executive Vice
# President" becomes "EVP", not "Executive VP".
_OFFICER_PATTERNS = [
    (
        short,
        re.compile(
            rf"(?<![\w-]){r'[\s-]+'.join(re.escape(w) for w in long.split())}(?![\w-])",
            re.IGNORECASE,
        ),
    )
    for short, long in sorted(OFFICER_ABBREVIATIONS.items(), key=lambda item: -len(item[1]))
]
# Words that stay small in a title in capitals: "CHIEF OF STAFF" is "Chief of Staff".
_TITLE_SMALL_WORDS = _SMALL_WORDS | {"to", "in", "on", "at"}


def company_name(name: str) -> str:
    """A company's name for display: without a state tag, and title-cased if in capitals."""
    cleaned = _strip_state_tag(name)
    return _title_case(cleaned.split()) if _in_capitals(cleaned) else cleaned


def display_name(name: str) -> str:
    """A Form 4 reporting owner's name for display. SEC lists people as "LAST FIRST MIDDLE"; they
    are shown as "First Middle Last", with any suffix last. Entities keep their word order. Names
    in capitals are title-cased, and a trailing state tag such as "/DE/" is dropped."""
    cleaned = _strip_state_tag(name)
    capitals = _in_capitals(cleaned)
    tokens = cleaned.split()
    if len(tokens) < 2 or any(t.upper().strip(".,") in _ENTITY_WORDS for t in tokens):
        return _title_case(tokens) if capitals else cleaned
    tokens = [t.rstrip(",") for t in tokens]
    # The last name is tokens[: end + 1]: particles and the word after them. At least one token
    # is left for the given names.
    end = 0
    while end + 2 < len(tokens) and _key(tokens[end]) in (
        _FIRST_PARTICLES if end == 0 else _NEXT_PARTICLES
    ):
        end += 1
    last, rest = tokens[: end + 1], tokens[end + 1 :]
    given = [t for t in rest if _key(t) not in _SUFFIXES]
    suffixes = [t for t in rest if _key(t) in _SUFFIXES]
    if not capitals:
        return " ".join([*given, *last, *suffixes])
    return " ".join([_title_case([*given, *last]), *(_suffix(t) for t in suffixes)])


def relationship_label(relationship: OwnerRelationship) -> str | None:
    """A Form 4 reporting owner's relationship to the issuer, short: "CFO", "Director",
    "10% owner", "Other", or several, most specific first: the officer title, "Director",
    "10% owner", then the other description ("President and CEO, Director"). That is the Form 4
    template's order too. None when the filing ticks no box."""
    parts: list[str] = []
    if relationship.is_officer:
        title = relationship.officer_title
        parts.append(officer_title(title) if title and title.strip() else "Officer")
    if relationship.is_director:
        parts.append("Director")
    if relationship.is_ten_percent_owner:
        parts.append("10% owner")
    if relationship.is_other:
        text = relationship.other_text
        parts.append(officer_title(text) if text and text.strip() else "Other")
    return ", ".join(parts) or None


def officer_title(title: str) -> str:
    """An officer title for display: the usual long forms abbreviated ("Executive Vice
    President, Chief Financial Officer" is "EVP, CFO"; OFFICER_ABBREVIATIONS), runs of spaces
    collapsed, and a title in capitals title-cased but for its abbreviations ("EVP: CHIEF
    OPERATING OFFICER" is "EVP: COO", "GENERAL COUNSEL" is "General Counsel"). Anything else
    stays as typed ("SVP, GC and Government Affairs")."""
    shown = " ".join(title.split())
    capitals = _in_capitals(shown)  # before abbreviating, which adds capitals
    for short, pattern in _OFFICER_PATTERNS:
        shown = pattern.sub(short, shown)
    if not capitals:
        return shown
    return " ".join(
        _title_word_or_abbreviation(w, first=i == 0) for i, w in enumerate(shown.split())
    )


def _strip_state_tag(name: str) -> str:
    # EDGAR conformed names can end in a state or status tag: "BANK OF AMERICA CORP /DE/".
    return re.sub(r"\s*/[A-Z]{2,3}/?\s*$", "", name.strip())


def _in_capitals(text: str) -> bool:
    """All capitals, or all capitals but for short words: EDGAR has "ELI LILLY & Co"."""
    words = [w for w in re.findall(r"[^\W\d_]+", text) if len(w) >= 3]
    return text.isupper() or (bool(words) and all(w.isupper() for w in words))


def _key(token: str) -> str:
    return token.upper().replace(".", "")


def _title_case(words: Sequence[str]) -> str:
    shown: list[str] = []
    for i, word in enumerate(words):
        if i > 0 and word == "COM":  # EDGAR writes Amazon.com as "AMAZON COM INC"
            shown[-1] += ".com"
        else:
            shown.append(_title_word(word, first=i == 0))
    return " ".join(shown)


def _title_word(word: str, *, first: bool) -> str:
    if word != word.upper():
        return word  # typed in mixed case: "Co" in "ELI LILLY & Co", "3i" in "3i GROUP PLC"
    if (core := word.strip(".,")) in _SPELLINGS:
        return word.replace(core, _SPELLINGS[core], 1)
    lower = word.lower()
    if not first and lower in _SMALL_WORDS:
        return lower
    if word.strip(".,") in _UPPER_WORDS or _AMPERSAND_WORD.fullmatch(word.strip(".,")):
        return word
    # Each run of letters capitalized: "O'Brien", "Smith-Jones". A possessive s stays small.
    titled = re.sub(r"[a-z]+", lambda m: m[0].capitalize(), lower)
    titled = re.sub(r"'S\b", "'s", titled)
    # Mc names: McDonald, McInerney, McNabb.
    return re.sub(r"^Mc([a-z])(?=[a-z])", lambda m: f"Mc{m[1].upper()}", titled)


def _title_word_or_abbreviation(word: str, *, first: bool) -> str:
    """A word of a title in capitals. Up to three letters, or none of them a vowel, is taken for
    an abbreviation and kept ("EVP", "UHG", "R&D", "MKTG"), unless a small word ("OF")."""
    letters = re.sub(r"[^A-Za-z]", "", word)
    if letters.lower() in _TITLE_SMALL_WORDS:
        return _title_word(word, first=first)
    if letters and (len(letters) <= 3 or not re.search("[AEIOU]", letters)):
        return word
    return _title_word(word, first=first)


def _suffix(token: str) -> str:
    """How a suffix in capitals is shown: "JR" as "Jr", "JR." as "Jr.", "III" as is."""
    shown = _SUFFIXES[_key(token)]
    if "." not in token:
        return shown
    return f"{shown}." if shown in ("Jr", "Sr") else token
