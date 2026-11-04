"""Amended filings (Form 4/A, 8-K/A): link each amendment to the filing it amends, and correct
insider numbers line by line.

An amendment is collected, parsed, and stored under its own accession number like any filing.
Saving its key fields then links it (`filings.amends_accession_no`) to the original Form 4 or
8-K, and a Form 4/A marks the original's transaction lines it restates
(`form4_transactions.superseded_by`), so totals count each trade once, as corrected.

**Linking a Form 4/A.** The amendment gives the original's filing date
(`dateOfOriginalSubmission`). The original is a Form 4 about the same issuer, filed that day
(or accepted that day, Eastern time), sharing a reporting owner. When several match, the one
sharing the most transaction lines wins.

**Linking an 8-K/A.** By the date its text gives ("...Current Report on Form 8-K filed on April
20, 2026"), else by EDGAR's report date (the date of the event both report), among the
company's 8-Ks filed before it. One reporting the same item (other than 9.01, exhibits) or the
same report date wins.

Candidates that can't be told apart leave the amendment unlinked rather than guess. So does an
original that isn't collected (older than the watchlist, say): the amendment is still stored,
parsed, and shown, with the date it gives.

**Correcting the lines.** Filers amend in three ways (all seen in the watchlist's 13 Form 4/As
of the year to 2026-11): restating the whole filing with one figure fixed, filing only the
corrected lines, or adding a trade the original left out. So each amendment line takes the
place of the matching current line (same transaction date, code, acquired or disposed,
security, table, and ownership), lines the amendment doesn't mention stay, and lines with no
match are new. Among several lines with the same key (a day's sales at different prices), an
amendment line pairs first with an identical one (same shares and price), then with one that
has the same price or the same shares, then in document order. Amendments apply in the order
they were filed, so a second amendment can correct the first. A line the original reported in
error and the amendment drops can't be told from one a partial amendment didn't mention: it
stays.

Saving any Form 4 recomputes its whole family (the original and every amendment linked to it)
from the rows in Postgres, so the result doesn't depend on the order filings arrive in, and
running it again changes nothing. An advisory lock per issuer (per filer for 8-Ks) makes
filings of the same company take turns, so an original and its amendment parsed at the same
moment still find each other.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from filings_common.amendments import LineKey, is_amendment, line_key
from filings_common.db import Filing, Form4Filing, Form4Transaction, Form8KFiling
from filings_common.parsed import EASTERN

EXHIBITS_ITEM = "9.01"


@dataclass(frozen=True)
class Line:
    accession_no: str
    line: int
    key: LineKey
    shares: float | None
    price_per_share: float | None

    @property
    def id(self) -> tuple[str, int]:
        return (self.accession_no, self.line)


def superseded_lines(
    original: Sequence[Line], amendments: Sequence[tuple[str, Sequence[Line]]]
) -> dict[tuple[str, int], str]:
    """Which lines the amendments replace, (accession number, line) -> the amendment's
    accession number. `amendments` in the order they were filed, each with its lines."""
    current: dict[LineKey, list[Line]] = {}
    for line in original:
        current.setdefault(line.key, []).append(line)
    marks: dict[tuple[str, int], str] = {}
    for accession_no, lines in amendments:
        restating: dict[LineKey, list[Line]] = {}
        for line in lines:
            restating.setdefault(line.key, []).append(line)
        for key, new in restating.items():
            replaced = _paired(current.get(key, []), new)
            for line in replaced:
                marks[line.id] = accession_no
            gone = {line.id for line in replaced}
            current[key] = [line for line in current.get(key, []) if line.id not in gone] + new
    return marks


def _paired(old: Sequence[Line], new: Sequence[Line]) -> list[Line]:
    """The old lines (all with one key) that the new lines replace, one each at most."""
    remaining = list(old)
    replaced: list[Line] = []
    unmatched: list[Line] = []
    for line in new:
        same = next(
            (
                o
                for o in remaining
                if (o.shares, o.price_per_share) == (line.shares, line.price_per_share)
            ),
            None,
        )
        if same is None:
            unmatched.append(line)
            continue
        remaining.remove(same)
        replaced.append(same)
    for line in unmatched:
        if not remaining:
            break  # the rest are new trades
        # max() keeps the first of equals: document order breaks ties.
        closest = max(
            remaining,
            key=lambda o: (o.price_per_share == line.price_per_share, o.shares == line.shares),
        )
        remaining.remove(closest)
        replaced.append(closest)
    return replaced


# --- Postgres -----------------------------------------------------------------------------------


def lock_company(session: Session, kind: str, cik: int) -> None:
    """Held until the transaction ends: one save at a time per company and form family."""
    key = sa.func.hashtextextended(f"filings-amendments:{kind}:{cik}", 0)
    session.execute(sa.select(sa.func.pg_advisory_xact_lock(key)))


def _filed_on(day: date) -> sa.ColumnElement[bool]:
    """Filed on `day`, or accepted on it in Eastern time (filed the next business day when
    accepted after 5:30 p.m.). Filers give either as the original's date."""
    accepted_day = sa.cast(sa.func.timezone(str(EASTERN), Filing.accepted_at), sa.Date)
    return sa.or_(Filing.filed_at == day, accepted_day == day)


def _filing_days(filed_at: date, accepted_at: datetime | None) -> set[date]:
    days = {filed_at}
    if accepted_at is not None:
        days.add(accepted_at.astimezone(EASTERN).date())
    return days


def _best[T](scored: Iterable[tuple[Any, T]]) -> T | None:
    """The top candidate, or None when there is none or the top two score the same."""
    ranked = sorted(scored, key=lambda pair: pair[0], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return None
    return ranked[0][1]


def _set_link(session: Session, accession_no: str, original: str | None) -> set[str]:
    """Point the amendment at `original`. Returns the families whose lines may change: the one
    it left and the one it joined."""
    before = session.scalar(
        sa.select(Filing.amends_accession_no).where(Filing.accession_no == accession_no)
    )
    if before != original:
        session.execute(
            sa.update(Filing)
            .where(Filing.accession_no == accession_no)
            .values(amends_accession_no=original)
        )
    return {a for a in (before, original) if a is not None}


# --- Form 4 -------------------------------------------------------------------------------------


def form4_saved(session: Session, accession_no: str, amends_filed_at: date | None) -> None:
    """After a Form 4 or 4/A's key fields are written (its lines freshly, without marks): link
    what can be linked, then recompute the lines of every family that may have changed. The
    caller holds `lock_company(session, "4", issuer)`."""
    filing = session.execute(
        sa.select(Filing.form_type, Filing.filed_at, Filing.accepted_at, Form4Filing.issuer_cik)
        .join(Form4Filing, Form4Filing.accession_no == Filing.accession_no)
        .where(Filing.accession_no == accession_no)
    ).one_or_none()
    if filing is None:
        return
    if is_amendment(filing.form_type):
        session.execute(
            sa.update(Filing)
            .where(Filing.accession_no == accession_no)
            .values(amends_filed_at=amends_filed_at)
        )
        families = _relink_form4(session, accession_no)
    else:
        # This filing may be the original that amendments already parsed were looking for.
        families = {accession_no}
        amendments = session.scalars(
            sa.select(Filing.accession_no)
            .join(Form4Filing, Form4Filing.accession_no == Filing.accession_no)
            .where(
                Filing.form_type == "4/A",
                Form4Filing.issuer_cik == filing.issuer_cik,
                Filing.amends_filed_at.in_(_filing_days(filing.filed_at, filing.accepted_at)),
            )
        ).all()
        for amendment in amendments:
            families |= _relink_form4(session, amendment)
    for original in sorted(families):
        _mark_superseded(session, original)


def _relink_form4(session: Session, accession_no: str) -> set[str]:
    return _set_link(session, accession_no, _form4_original(session, accession_no))


def _form4_original(session: Session, accession_no: str) -> str | None:
    amendment = session.execute(
        sa.select(Filing.amends_filed_at, Form4Filing.issuer_cik, Form4Filing.reporting_owner_ciks)
        .join(Form4Filing, Form4Filing.accession_no == Filing.accession_no)
        .where(Filing.accession_no == accession_no)
    ).one()
    if amendment.amends_filed_at is None or not amendment.reporting_owner_ciks:
        return None
    candidates = session.scalars(
        sa.select(Filing.accession_no)
        .join(Form4Filing, Form4Filing.accession_no == Filing.accession_no)
        .where(
            Filing.form_type == "4",
            Filing.accession_no != accession_no,
            Form4Filing.issuer_cik == amendment.issuer_cik,
            Form4Filing.reporting_owner_ciks.overlap(amendment.reporting_owner_ciks),
            _filed_on(amendment.amends_filed_at),
        )
    ).all()
    if not candidates:
        return None
    lines = _lines(session, [accession_no, *candidates])
    restated = {line.key for line in lines.get(accession_no, [])}
    return _best((len(restated & {line.key for line in lines.get(c, [])}), c) for c in candidates)


def _mark_superseded(session: Session, original: str) -> None:
    """Recompute `superseded_by` on the lines of a Form 4 and its amendments."""
    amendments = session.scalars(
        sa.select(Filing.accession_no)
        .where(Filing.amends_accession_no == original, Filing.form_type == "4/A")
        .order_by(Filing.filed_at, Filing.accepted_at, Filing.accession_no)
    ).all()
    family = [original, *amendments]
    current = session.execute(
        sa.select(
            Form4Transaction.accession_no, Form4Transaction.line, Form4Transaction.superseded_by
        ).where(Form4Transaction.accession_no.in_(family))
    ).all()
    lines = _lines(session, family)
    wanted = superseded_lines(lines.get(original, []), [(a, lines.get(a, [])) for a in amendments])
    for accession_no, line, mark in current:
        if wanted.get((accession_no, line)) != mark:
            session.execute(
                sa.update(Form4Transaction)
                .where(
                    Form4Transaction.accession_no == accession_no,
                    Form4Transaction.line == line,
                )
                .values(superseded_by=wanted.get((accession_no, line)))
            )


def _lines(session: Session, accession_nos: Sequence[str]) -> dict[str, list[Line]]:
    t = Form4Transaction
    rows = session.execute(
        sa.select(
            t.accession_no,
            t.line,
            t.transaction_date,
            t.code,
            t.acquired_disposed,
            t.security_title,
            t.is_derivative,
            t.direct_or_indirect,
            t.nature_of_ownership,
            t.shares,
            t.price_per_share,
        )
        .where(t.accession_no.in_(accession_nos))
        .order_by(t.accession_no, t.line)
    ).all()
    lines: dict[str, list[Line]] = {}
    for row in rows:
        key = line_key(
            row.transaction_date,
            row.code,
            row.acquired_disposed,
            row.security_title,
            row.is_derivative,
            row.direct_or_indirect,
            row.nature_of_ownership,
        )
        lines.setdefault(row.accession_no, []).append(
            Line(row.accession_no, row.line, key, row.shares, row.price_per_share)
        )
    return lines


# --- 8-K ----------------------------------------------------------------------------------------


def form8k_saved(session: Session, accession_no: str, amends_filed_at: date | None) -> None:
    """After an 8-K or 8-K/A's key fields are written: link what can be linked. The caller
    holds `lock_company(session, "8-K", cik)`."""
    filing = session.execute(
        sa.select(
            Filing.cik, Filing.form_type, Filing.filed_at, Filing.accepted_at, Filing.report_date
        ).where(Filing.accession_no == accession_no)
    ).one_or_none()
    if filing is None:
        return
    if is_amendment(filing.form_type):
        session.execute(
            sa.update(Filing)
            .where(Filing.accession_no == accession_no)
            .values(amends_filed_at=amends_filed_at)
        )
        _set_link(session, accession_no, _form8k_original(session, accession_no))
        return
    # This 8-K may be the original that amendments already parsed were looking for.
    refers: sa.ColumnElement[bool] = Filing.amends_filed_at.in_(
        _filing_days(filing.filed_at, filing.accepted_at)
    )
    if filing.report_date is not None:
        refers = sa.or_(refers, Filing.report_date == filing.report_date)
    amendments = session.scalars(
        sa.select(Filing.accession_no).where(
            Filing.cik == filing.cik,
            Filing.form_type == "8-K/A",
            Filing.filed_at >= filing.filed_at,
            refers,
        )
    ).all()
    for amendment in amendments:
        _set_link(session, amendment, _form8k_original(session, amendment))


def _form8k_original(session: Session, accession_no: str) -> str | None:
    amendment = session.execute(
        sa.select(
            Filing.cik,
            Filing.filed_at,
            Filing.report_date,
            Filing.amends_filed_at,
            Form8KFiling.items,
        )
        .outerjoin(Form8KFiling, Form8KFiling.accession_no == Filing.accession_no)
        .where(Filing.accession_no == accession_no)
    ).one()
    items = set(amendment.items or []) - {EXHIBITS_ITEM}
    ways = []
    if amendment.amends_filed_at is not None:
        ways.append(_filed_on(amendment.amends_filed_at))
    if amendment.report_date is not None:
        ways.append(Filing.report_date == amendment.report_date)
    for way in ways:  # the text's date first; the report date when no 8-K was filed that day
        candidates = session.execute(
            sa.select(Filing.accession_no, Filing.report_date, Form8KFiling.items)
            .outerjoin(Form8KFiling, Form8KFiling.accession_no == Filing.accession_no)
            .where(
                Filing.cik == amendment.cik,
                Filing.form_type == "8-K",
                Filing.filed_at <= amendment.filed_at,
                way,
            )
        ).all()
        if candidates:
            return _best(
                (
                    (
                        bool(items & set(c.items or [])),
                        amendment.report_date is not None
                        and c.report_date == amendment.report_date,
                    ),
                    c.accession_no,
                )
                for c in candidates
            )
    return None
