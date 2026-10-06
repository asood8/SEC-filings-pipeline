"""Deterministic Form 4 summaries. A Form 4 is already structured data, so no LLM is needed.

Transactions are grouped by kind (SEC transaction code, direction, and for derivatives the
security), and each group becomes one plain-English sentence. Prices given only in footnotes stay
unknown; the template never guesses.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from filings_common.parsed import Form4, ReportingOwner, Transaction
from filings_common.summaries import FilingSummary

TEMPLATE_VERSION = 1
PROVIDER = "template"
MODEL = "form4-template"

MAX_GROUP_SENTENCES = 4

# Words that mark a reporting owner as an entity rather than a person.
_ENTITY_WORDS = frozenset(
    [
        "INC",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LLC",
        "LP",
        "LLP",
        "LTD",
        "PLC",
        "TRUST",
        "FUND",
        "FUNDS",
        "HOLDINGS",
        "PARTNERS",
        "GROUP",
        "BANK",
        "BANC",
        "CAPITAL",
        "MANAGEMENT",
        "ADVISORS",
        "FOUNDATION",
        "N.A",
    ]
)
_NAME_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV"})
_SMALL_WORDS = frozenset({"of", "and", "the", "for", "&"})

# Non-derivative transactions, by SEC transaction code. {qty} is e.g. "1,000 AMZN shares".
_PHRASES = {
    "P": "bought {qty}",
    "S": "sold {qty}",
    "A": "received {qty} as a grant or award",
    "M": "acquired {qty} by exercising or converting derivative securities",
    "X": "acquired {qty} by exercising options",
    "C": "acquired {qty} through a conversion",
    "F": "had {qty} withheld to pay taxes or an exercise price",
    "G": "gave {qty} as a gift",
    "D": "disposed of {qty} to the issuer",
    "J": "reported {qty} in another type of transaction (explained in footnotes)",
    "W": "acquired or disposed of {qty} by will or inheritance",
    "I": "made a discretionary transaction in {qty}",
}
# Derivative transactions. {qty} is e.g. "720 Non-Qualified Stock Option (right to buy) (NFLX)".
_DERIVATIVE_PHRASES = {
    "A": "received {qty}",
    "M": "exercised or converted {qty}",
    "X": "exercised {qty}",
    "C": "converted {qty}",
    "G": "gave {qty} as a gift",
}
# Codes whose price is a market price, so shares x price is a meaningful value.
_VALUED_AT_MARKET = frozenset({"F", "D", "J"})
# Which group leads the headline: open-market trades first, then the most telling events.
_HEADLINE_PRIORITY = ["P", "S", "M", "X", "C", "A", "G", "F", "D", "J"]


def _title_case(text: str) -> str:
    words = text.lower().split()
    return " ".join(
        word if i and word in _SMALL_WORDS else re.sub(r"[a-z]+", lambda m: m[0].capitalize(), word)
        for i, word in enumerate(words)
    )


def display_name(name: str) -> str:
    """SEC lists people as "LAST FIRST MIDDLE"; show "First Middle Last". Entities keep their name.

    All-caps names are title-cased. A trailing state tag such as "/DE/" is dropped.
    """
    cleaned = _strip_state_tag(name)
    tokens = cleaned.split()
    if len(tokens) >= 2 and not any(t.upper().strip(".,") in _ENTITY_WORDS for t in tokens):
        suffixes = [t for t in tokens[1:] if t.upper().rstrip(".") in _NAME_SUFFIXES]
        given = [t for t in tokens[1:] if t not in suffixes]
        tokens = [*given, tokens[0], *suffixes]
    joined = " ".join(tokens)
    return _title_case(joined) if joined.isupper() else joined


def _strip_state_tag(name: str) -> str:
    # EDGAR conformed names can end in a state or status tag: "BANK OF AMERICA CORP /DE/".
    return re.sub(r"\s*/[A-Z]{2,3}/?\s*$", "", name.strip())


def company_name(name: str) -> str:
    """An issuer's name for display: without a state tag, and title-cased if all caps."""
    cleaned = _strip_state_tag(name)
    return _title_case(cleaned) if cleaned.isupper() else cleaned


def _role(owner: ReportingOwner) -> str | None:
    roles = []
    if owner.is_officer:
        roles.append(owner.officer_title or "Officer")
    if owner.is_director:
        roles.append("Director")
    if owner.is_ten_percent_owner:
        roles.append("10% owner")
    if owner.is_other and owner.other_text:
        roles.append(owner.other_text)
    return ", ".join(roles) or None


def _who(owners: Sequence[ReportingOwner]) -> str:
    names = [display_name(o.name) for o in owners]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} and {len(names) - 1} others"


def _who_with_roles(owners: Sequence[ReportingOwner]) -> str:
    first = owners[0]
    role = _role(first)
    label = f"{display_name(first.name)} ({role})" if role else display_name(first.name)
    if len(owners) == 2:
        return f"{label} and {display_name(owners[1].name)}"
    if len(owners) > 2:
        return f"{label} and {len(owners) - 1} other reporting owners"
    return label


def _number(value: float) -> str:
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def _money(value: float) -> str:
    for threshold, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= threshold:
            return f"${value / threshold:.1f}{unit}"
    return f"${value:,.2f}"


def _date(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def _date_range(days: Sequence[date]) -> str:
    if not days:
        return ""
    first, last = min(days), max(days)
    if first == last:
        return f"on {_date(first)}"
    return f"between {_date(first)} and {_date(last)}"


@dataclass
class _Group:
    derivative: bool
    code: str
    acquired_disposed: str | None
    security_title: str
    rows: list[Transaction] = field(default_factory=list)

    @property
    def shares(self) -> float:
        return sum(r.shares or 0 for r in self.rows)

    @property
    def value(self) -> float | None:
        """Total value, only when every row has a positive price (awards and gifts have none)."""
        if not self.rows or any(not r.price_per_share or r.shares is None for r in self.rows):
            return None
        return sum((r.shares or 0) * (r.price_per_share or 0) for r in self.rows)

    @property
    def days(self) -> list[date]:
        return [r.transaction_date for r in self.rows if r.transaction_date]


def _groups(transactions: Sequence[Transaction]) -> list[_Group]:
    groups: dict[tuple[bool, str, str | None, str], _Group] = {}
    for t in transactions:
        code = t.code or "?"
        key = (t.derivative, code, t.acquired_disposed, t.security_title)
        group = _Group(t.derivative, code, t.acquired_disposed, t.security_title)
        groups.setdefault(key, group).rows.append(t)
    return list(groups.values())


def _phrase(group: _Group, ticker: str, *, name_security: bool = False) -> str:
    """e.g. "sold 1,000 AMZN shares for $251.5K (avg $251.50/share)".

    With `name_security` (several share classes in one filing), stock is named by its title.
    """
    if group.derivative:
        qty = f"{_number(group.shares)} {group.security_title} ({ticker})"
        template = _DERIVATIVE_PHRASES.get(group.code)
    else:
        if name_security:
            qty = f"{_number(group.shares)} shares of {group.security_title} ({ticker})"
        else:
            qty = f"{_number(group.shares)} {ticker} shares"
        template = _PHRASES.get(group.code)
        if group.code == "G" and group.acquired_disposed == "A":
            template = "received {qty} as a gift"
    if template is None:
        verb = "acquired" if group.acquired_disposed == "A" else "disposed of"
        template = f"{verb} {{qty}} (transaction code {group.code})"
    text = template.format(qty=qty)

    # Exercises and conversions report the strike price, not a market value, so they get none.
    value = None if group.derivative else group.value
    if value is not None and group.code in ("P", "S"):
        text += f" for {_money(value)} (avg ${value / group.shares:,.2f}/share)"
    elif value is not None and group.code in _VALUED_AT_MARKET:
        text += f" worth {_money(value)}"
    return text


def _headline_group(groups: Sequence[_Group]) -> _Group:
    def rank(group: _Group) -> tuple[int, float]:
        order = (
            _HEADLINE_PRIORITY.index(group.code)
            if group.code in _HEADLINE_PRIORITY
            else len(_HEADLINE_PRIORITY)
        )
        # Among open-market trades, the bigger one leads.
        return (min(order, 1) if group.code in ("P", "S") else order, -(group.value or 0))

    return min(groups, key=rank)


def _holdings_after(form: Form4, ticker: str) -> str | None:
    """The holding after the last trade, only if every trade is in one ownership line.

    With several share classes or holding entities, the last row's balance would be one of
    several, so no sentence is better than a misleading one.
    """
    stock = [t for t in form.transactions if not t.derivative]
    lines = {(t.security_title, t.direct_or_indirect, t.nature_of_ownership) for t in stock}
    if len(lines) != 1:
        return None
    last = stock[-1]
    if last.shares_owned_after is None:
        return None
    how = {"D": " directly", "I": " indirectly"}.get(last.direct_or_indirect or "", "")
    held = _number(last.shares_owned_after)
    return f"Afterwards, the reported holding was {held} {ticker} shares{how}"


def summarize_form4(form: Form4) -> FilingSummary:
    issuer = company_name(form.issuer.name)
    ticker = (form.issuer.trading_symbol or "").split(",")[0].strip() or issuer
    groups = _groups(form.transactions)
    who = _who(form.reporting_owners)
    stock_titles = {t.security_title for t in form.transactions if not t.derivative}
    name_security = len(stock_titles) > 1

    def phrase(group: _Group) -> str:
        return _phrase(group, ticker, name_security=name_security)

    lead = _headline_group(groups)
    headline = f"{who} {phrase(lead)}"
    if len(groups) > 1:
        headline += f" (+{len(groups) - 1} more)"

    count = len(form.transactions)
    days = [d for g in groups for d in g.days]
    when = _date_range(days) or f"for the period ending {_date(form.period_of_report)}"
    noun = "transaction" if count == 1 else "transactions"
    sentences = [
        f"{_who_with_roles(form.reporting_owners)} reported {count} {noun} "
        f"in {issuer} ({ticker}) securities {when}."
    ]
    sentences += [f"{who} {phrase(g)}." for g in groups[:MAX_GROUP_SENTENCES]]
    if len(groups) > MAX_GROUP_SENTENCES:
        sentences.append(
            f"The filing reports {len(groups) - MAX_GROUP_SENTENCES} more kinds of transaction."
        )
    if form.aff10b5_one:
        sentences.append("The filing says the trades were made under a Rule 10b5-1 trading plan.")
    if (holding := _holdings_after(form, ticker)) is not None:
        sentences.append(f"{holding}.")

    key_points = [f"{phrase(g)[0].upper()}{phrase(g)[1:]} {_date_range(g.days)}" for g in groups]
    if form.aff10b5_one:
        key_points.append("Made under a Rule 10b5-1 trading plan")
    if any(t.code in ("P", "S") and t.price_per_share is None for t in form.transactions):
        key_points.append("Some prices are given only in the filing's footnotes")
    if len(form.reporting_owners) > 1:
        key_points.append(f"Filed jointly by {len(form.reporting_owners)} reporting owners")

    headline = _squash(headline)
    return FilingSummary(
        headline=headline if len(headline) <= 200 else headline[:197] + "...",
        summary=_squash(" ".join(sentences)),
        key_points=[_squash(point) for point in key_points[:8]],
    )


def _squash(text: str) -> str:
    # Filed text sometimes has doubled spaces ("Senior Vice President,  CFO").
    return " ".join(text.split())
