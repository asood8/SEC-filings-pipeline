"""Amended filings (filings_parser.amendments): parsing Form 4/As and 8-K/As, matching an
amendment's lines to the original's, and linking amendments in Postgres.

Uses real EDGAR documents: tests/fixtures/form4/amendments/ holds Form 4/As filed by or about
watchlist companies in the year to 2026-11, with the Form 4s they amend (and some filed the same
day that they don't); tests/fixtures/form8k/amendments/ holds 8-K/As. The 8-Ks they amend are
made up here: only their dates and items matter."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pymongo.database import Database
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from filings_common.amendments import amendment_reason, changes_nothing, line_key
from filings_common.db import (
    Company,
    Filing,
    FilingStatus,
    Form4Transaction,
    session_factory,
)
from filings_common.mongo import ParsedFilingStore
from filings_parser import form4, form8k
from filings_parser.amendments import Line, superseded_lines
from filings_parser.form4 import parse_form4
from filings_parser.rebuild import rebuild_key_fields

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures"
FORM4 = FIXTURES / "form4" / "amendments"
FORM8K = FIXTURES / "form8k" / "amendments"

# Form 4/As, by what they do, with the Form 4 each amends.
GIFT_FIX = "0001193125-25-272571"  # Alphabet director: corrects one trust's gift, not the other's
GIFT_ORIGINAL = "0001193125-25-223743"
PSU_FIX = "0001193125-26-061630"  # Alphabet officer: shares acquired 47,574 -> 94,715
PSU_ORIGINAL = "0001193125-26-014384"
PSU_FIX_2 = "0001193125-26-061779"  # the same fix, for another officer
PSU_ORIGINAL_2 = "0001193125-26-014381"
SAME_DAY = ["0001193125-26-014378", "0001193125-26-014386"]  # other officers' Form 4s that day
ADDS_TRADES = "0000886982-26-000522"  # Goldman: restates 29 lines, adds 6 it left out
ADDS_TRADES_ORIGINAL = "0000886982-26-000495"
OMITTED_SALE = "0001193125-26-007679"  # J&J as a 10% owner: adds a sale the original omitted
OMITTED_SALE_ORIGINAL = "0001193125-25-303876"
TAX_FIX = "0001860253-26-000006"  # Lilly officer: shares withheld for tax, corrected
TAX_ORIGINAL = "0001860253-26-000004"
OTHER_LILLY = "0001538604-26-000004"  # another Lilly officer's Form 4 the same day
PRICE_FIX = "0000320187-26-000053"  # Nike director: purchase price 42.265 -> 42.27
PRICE_ORIGINAL = "0000320187-26-000051"
POWER_OF_ATTORNEY = "0000320187-26-000129"  # Nike: only attaches a power of attorney
POWER_OF_ATTORNEY_ORIGINAL = "0000320187-26-000055"
OLD_GIFT_FIX = "0001193125-26-188493"  # Alphabet: corrects a Form 4 filed in 2023

FILED = {  # filing dates, from EDGAR
    GIFT_FIX: date(2025, 11, 7),
    GIFT_ORIGINAL: date(2025, 9, 29),
    PSU_FIX: date(2026, 2, 20),
    PSU_FIX_2: date(2026, 2, 20),
    PSU_ORIGINAL: date(2026, 1, 15),
    PSU_ORIGINAL_2: date(2026, 1, 15),
    **dict.fromkeys(SAME_DAY, date(2026, 1, 15)),
    ADDS_TRADES: date(2026, 8, 28),
    ADDS_TRADES_ORIGINAL: date(2026, 8, 13),
    OMITTED_SALE: date(2026, 1, 8),
    OMITTED_SALE_ORIGINAL: date(2025, 12, 1),
    TAX_FIX: date(2026, 2, 12),
    TAX_ORIGINAL: date(2026, 2, 11),
    OTHER_LILLY: date(2026, 2, 11),
    PRICE_FIX: date(2026, 4, 22),
    PRICE_ORIGINAL: date(2026, 4, 14),
    POWER_OF_ATTORNEY: date(2026, 9, 3),
    POWER_OF_ATTORNEY_ORIGINAL: date(2026, 5, 15),
    OLD_GIFT_FIX: date(2026, 4, 28),
}
WATCHLIST_CIK = 1652044  # the company each test filing was found under (any would do)


def form4_payload(accession_no: str) -> dict[str, Any]:
    return form4.parse((FORM4 / f"{accession_no}.xml").read_bytes())


def lines_of(accession_no: str) -> list[Line]:
    """A fixture's lines as the amendments module reads them from Postgres."""
    form = parse_form4((FORM4 / f"{accession_no}.xml").read_bytes())
    return [
        Line(
            accession_no,
            number,
            line_key(
                t.transaction_date,
                t.code,
                t.acquired_disposed,
                t.security_title,
                t.derivative,
                t.direct_or_indirect,
                t.nature_of_ownership,
            ),
            t.shares,
            t.price_per_share,
        )
        for number, t in enumerate(form.transactions, start=1)
    ]


# --- Parsing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(FORM4.glob("*.xml")), ids=lambda p: p.stem)
def test_amendments_give_the_original_date_and_originals_none(path: Path) -> None:
    form = parse_form4(path.read_bytes())
    if form.document_type == "4/A":
        assert form.date_of_original_submission is not None
    else:
        assert (form.document_type, form.date_of_original_submission) == ("4", None)


def test_form4_amendment_dates() -> None:
    assert {
        a: parse_form4((FORM4 / f"{a}.xml").read_bytes()).date_of_original_submission
        for a in (GIFT_FIX, PSU_FIX, ADDS_TRADES, OLD_GIFT_FIX)
    } == {
        GIFT_FIX: FILED[GIFT_ORIGINAL],
        PSU_FIX: FILED[PSU_ORIGINAL],
        ADDS_TRADES: FILED[ADDS_TRADES_ORIGINAL],
        OLD_GIFT_FIX: date(2023, 3, 28),
    }


@pytest.mark.parametrize(
    ("accession_no", "items", "amends_filed_at"),
    [
        ("0001140361-26-035325", ["5.02"], date(2026, 4, 20)),  # Apple: "...Form 8-K filed on"
        ("0000093410-25-000130", ["5.02"], date(2025, 7, 29)),  # Chevron
        ("0000063908-26-000042", ["5.02"], date(2026, 2, 10)),  # McDonald's: "...that X filed on"
        ("0001193125-25-309911", ["9.01"], date(2025, 12, 5)),  # Netflix: in its note
        ("0000104169-26-000024", ["5.02"], date(2025, 11, 14)),  # Walmart: "...filed with the SEC"
        ("0000104169-26-000028", ["5.02"], date(2026, 1, 16)),
        ("0001193125-26-004727", ["5.02"], None),  # Berkshire: gives no date
    ],
)
def test_8k_amendments_give_their_items_and_the_amended_date(
    accession_no: str, items: list[str], amends_filed_at: date | None
) -> None:
    payload = form8k.parse_amendment((FORM8K / f"{accession_no}.htm").read_bytes())
    assert [item["number"] for item in payload["items"]] == items
    assert payload["amends_filed_at"] == (amends_filed_at and amends_filed_at.isoformat())


def test_an_8k_amendments_explanatory_note_is_kept() -> None:
    payload = form8k.parse_amendment((FORM8K / "0001193125-25-309911.htm").read_bytes())
    assert payload["explanatory_note"].startswith(
        "This Amendment on Form 8-K/A (this “Amendment”) amends the Current Report on Form 8-K"
    )
    # An 8-K/A without one, and an ordinary 8-K parse, keep none.
    apple = form8k.parse_amendment((FORM8K / "0001140361-26-035325.htm").read_bytes())
    assert apple["explanatory_note"] is None
    plain = form8k.parse((FORM8K / "0001193125-25-309911.htm").read_bytes())
    assert (plain["explanatory_note"], plain["amends_filed_at"]) == (None, None)


# --- Matching lines -----------------------------------------------------------------------------


def test_a_partial_amendment_replaces_only_the_lines_it_restates() -> None:
    # The original reports two trusts' gifts (lines 1-2 and 5-6, then 3-4 and 7-8); the
    # amendment restates the second trust's four lines with 121,744 shares, not 121,764.
    marks = superseded_lines(lines_of(GIFT_ORIGINAL), [(GIFT_FIX, lines_of(GIFT_FIX))])
    assert marks == {(GIFT_ORIGINAL, n): GIFT_FIX for n in (3, 4, 7, 8)}


def test_a_corrected_figure_replaces_its_line_and_the_rest_stay() -> None:
    marks = superseded_lines(lines_of(PSU_ORIGINAL), [(PSU_FIX, lines_of(PSU_FIX))])
    # Line 1, the award, is corrected; line 2, the shares withheld for tax, stays as filed.
    assert marks == {(PSU_ORIGINAL, 1): PSU_FIX}
    for original, fix in ((TAX_ORIGINAL, TAX_FIX), (PRICE_ORIGINAL, PRICE_FIX)):
        assert superseded_lines(lines_of(original), [(fix, lines_of(fix))]) == {(original, 1): fix}


def test_trades_an_amendment_adds_count_alongside_the_ones_it_restates() -> None:
    original, amendment = lines_of(ADDS_TRADES_ORIGINAL), lines_of(ADDS_TRADES)
    marks = superseded_lines(original, [(ADDS_TRADES, amendment)])
    assert marks == {line.id: ADDS_TRADES for line in original}  # all 29 restated
    assert len(amendment) - len(marks) == 6  # and 6 added
    # J&J's: the sale it left out, plus a restated line.
    original, amendment = lines_of(OMITTED_SALE_ORIGINAL), lines_of(OMITTED_SALE)
    assert superseded_lines(original, [(OMITTED_SALE, amendment)]) == {
        (OMITTED_SALE_ORIGINAL, 1): OMITTED_SALE
    }


def test_a_paperwork_amendment_restates_the_line_unchanged() -> None:
    original = lines_of(POWER_OF_ATTORNEY_ORIGINAL)
    [restated] = lines_of(POWER_OF_ATTORNEY)
    assert superseded_lines(original, [(POWER_OF_ATTORNEY, [restated])]) == {
        (POWER_OF_ATTORNEY_ORIGINAL, 1): POWER_OF_ATTORNEY
    }
    assert (restated.shares, restated.price_per_share) == (
        original[0].shares,
        original[0].price_per_share,
    )


def test_among_lines_with_one_key_identical_ones_pair_first_then_by_price() -> None:
    key = line_key(date(2026, 9, 1), "S", "D", "Common Stock", False, "D", None)
    original = [
        Line("O", 1, key, 100, 10.0),
        Line("O", 2, key, 200, 11.0),
        Line("O", 3, key, 300, 12.0),
    ]
    # Only the 11.00 line, with its shares corrected: the 11.00 line goes.
    assert superseded_lines(original, [("A", [Line("A", 1, key, 250, 11.0)])]) == {("O", 2): "A"}
    # Everything restated, in another order, plus one more sale: the extra one is new.
    restated = [replace(line, accession_no="A") for line in reversed(original)]
    marks = superseded_lines(original, [("A", [*restated, Line("A", 4, key, 50, 13.0)])])
    assert marks == {("O", 1): "A", ("O", 2): "A", ("O", 3): "A"}


def test_a_second_amendment_corrects_the_first() -> None:
    key = line_key(date(2026, 9, 1), "P", "A", "Common Stock", False, "D", None)
    marks = superseded_lines(
        [Line("O", 1, key, 100, 10.0)],
        [("A1", [Line("A1", 1, key, 150, 10.0)]), ("A2", [Line("A2", 1, key, 120, 10.0)])],
    )
    assert marks == {("O", 1): "A1", ("A1", 1): "A2"}


def test_keys_ignore_case_and_spacing_of_text() -> None:
    day = date(2026, 9, 1)
    assert line_key(day, "S", "D", "COMMON  STOCK", False, "I", "By Trust ") == line_key(
        day, "S", "D", "Common Stock", False, "I", "by trust"
    )


# --- Linking in Postgres ------------------------------------------------------------------------


@pytest.fixture
def sessions(clean_db: Engine) -> sessionmaker[Session]:
    factory = session_factory(clean_db)
    with factory.begin() as session:
        session.add(Company(cik=WATCHLIST_CIK, ticker="GOOGL", name="Alphabet Inc."))
    return factory


def add_filing(
    sessions: sessionmaker[Session],
    accession_no: str,
    form_type: str,
    filed_at: date,
    *,
    accepted_at: datetime | None = None,
    report_date: date | None = None,
) -> None:
    with sessions.begin() as session:
        session.add(
            Filing(
                accession_no=accession_no,
                cik=WATCHLIST_CIK,
                form_type=form_type,
                filed_at=filed_at,
                accepted_at=accepted_at,
                report_date=report_date,
                primary_doc_url=f"https://www.sec.gov/x/{accession_no}",
                raw_object_key=f"filings/{WATCHLIST_CIK}/{accession_no}/doc",
                status=FilingStatus.PARSED,
            )
        )


def save_form4(
    sessions: sessionmaker[Session],
    accession_no: str,
    payload: dict[str, Any] | None = None,
    filed_at: date | None = None,
) -> None:
    """Record the filing (unless it is already) and save its key fields, as the parser does."""
    data = payload or form4_payload(accession_no)
    with sessions() as session:
        known = session.get(Filing, accession_no) is not None
    if not known:
        add_filing(sessions, accession_no, data["document_type"], filed_at or FILED[accession_no])
    with sessions.begin() as session:
        form4.save_key_fields(session, accession_no, data)


def links(sessions: sessionmaker[Session]) -> dict[str, tuple[date | None, str | None]]:
    """Each amendment's (amends_filed_at, amends_accession_no)."""
    with sessions() as session:
        return {
            accession_no: (filed_at, original)
            for accession_no, filed_at, original in session.execute(
                sa.select(Filing.accession_no, Filing.amends_filed_at, Filing.amends_accession_no)
                .where(Filing.form_type.like("%/A"))
                .order_by(Filing.accession_no)
            )
        }


def marks(sessions: sessionmaker[Session]) -> dict[tuple[str, int], str | None]:
    """Superseded lines: (accession number, line) -> the amendment that restates it."""
    with sessions() as session:
        return {
            (accession_no, line): mark
            for accession_no, line, mark in session.execute(
                sa.select(
                    Form4Transaction.accession_no,
                    Form4Transaction.line,
                    Form4Transaction.superseded_by,
                ).where(Form4Transaction.superseded_by.is_not(None))
            )
        }


def current_shares(sessions: sessionmaker[Session], code: str) -> float:
    """Shares of one code over every saved filing's current lines, as totals count them."""
    with sessions() as session:
        total = session.scalar(
            sa.select(sa.func.sum(Form4Transaction.shares)).where(
                Form4Transaction.code == code, Form4Transaction.superseded_by.is_(None)
            )
        )
    return float(total or 0)


@pytest.mark.integration
def test_each_amendment_links_to_its_owners_original_among_the_days_form4s(
    sessions: sessionmaker[Session],
) -> None:
    # Four Alphabet officers' Form 4s on 2026-01-15; two of them amended on 2026-02-20.
    for accession_no in [PSU_ORIGINAL, PSU_ORIGINAL_2, *SAME_DAY, PSU_FIX, PSU_FIX_2]:
        save_form4(sessions, accession_no)

    assert links(sessions) == {
        PSU_FIX: (date(2026, 1, 15), PSU_ORIGINAL),
        PSU_FIX_2: (date(2026, 1, 15), PSU_ORIGINAL_2),
    }
    # PSU_FIX restates only the award; PSU_FIX_2 restates both of its original's lines.
    assert marks(sessions) == {
        (PSU_ORIGINAL, 1): PSU_FIX,
        (PSU_ORIGINAL_2, 1): PSU_FIX_2,
        (PSU_ORIGINAL_2, 2): PSU_FIX_2,
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    "amendment_first", [False, True], ids=["original first", "amendment first"]
)
def test_the_result_is_the_same_whichever_arrives_first(
    sessions: sessionmaker[Session], amendment_first: bool
) -> None:
    order = [GIFT_ORIGINAL, GIFT_FIX, ADDS_TRADES_ORIGINAL, ADDS_TRADES]
    for accession_no in (order[1::-1] + order[:1:-1]) if amendment_first else order:
        save_form4(sessions, accession_no)

    assert links(sessions) == {
        ADDS_TRADES: (FILED[ADDS_TRADES_ORIGINAL], ADDS_TRADES_ORIGINAL),
        GIFT_FIX: (FILED[GIFT_ORIGINAL], GIFT_ORIGINAL),
    }
    assert marks(sessions) == {
        **{(GIFT_ORIGINAL, n): GIFT_FIX for n in (3, 4, 7, 8)},
        **{(ADDS_TRADES_ORIGINAL, n): ADDS_TRADES for n in range(1, 30)},
    }
    # Gifts: 121,764 shares x 4 for the first trust's lines, 121,744 x 4 for the corrected ones.
    assert current_shares(sessions, "G") == 4 * 121_764 + 4 * 121_744


@pytest.mark.integration
def test_saving_again_and_rebuilding_change_nothing(
    sessions: sessionmaker[Session], mongo_db: Database[dict[str, Any]]
) -> None:
    store = ParsedFilingStore(mongo_db)
    for accession_no in (OMITTED_SALE_ORIGINAL, OMITTED_SALE):
        save_form4(sessions, accession_no)
        store.upsert(
            accession_no=accession_no,
            cik=WATCHLIST_CIK,
            form_type="4",
            parser_version=form4.PARSER_VERSION,
            parsed=form4_payload(accession_no),
        )
    before = (links(sessions), marks(sessions), current_shares(sessions, "S"))
    assert before[0] == {OMITTED_SALE: (FILED[OMITTED_SALE_ORIGINAL], OMITTED_SALE_ORIGINAL)}
    assert before[1] == {(OMITTED_SALE_ORIGINAL, 1): OMITTED_SALE}

    save_form4(sessions, OMITTED_SALE)  # a replay
    save_form4(sessions, OMITTED_SALE_ORIGINAL)
    assert (links(sessions), marks(sessions), current_shares(sessions, "S")) == before
    assert rebuild_key_fields(sessions, store).rebuilt == 2
    assert (links(sessions), marks(sessions), current_shares(sessions, "S")) == before


@pytest.mark.integration
def test_an_amendment_whose_original_isnt_collected_stays_unlinked(
    sessions: sessionmaker[Session],
) -> None:
    save_form4(sessions, OLD_GIFT_FIX)  # amends a Form 4 from 2023
    assert links(sessions) == {OLD_GIFT_FIX: (date(2023, 3, 28), None)}
    assert marks(sessions) == {}
    assert current_shares(sessions, "G") == 2 * 34_801  # its own lines count


@pytest.mark.integration
def test_originals_that_cant_be_told_apart_leave_it_unlinked(
    sessions: sessionmaker[Session],
) -> None:
    # The same Form 4 under two accession numbers: the same owner, day, and lines.
    twin = "0001860253-26-000005"
    save_form4(sessions, TAX_ORIGINAL)
    save_form4(sessions, twin, form4_payload(TAX_ORIGINAL), FILED[TAX_ORIGINAL])
    save_form4(sessions, OTHER_LILLY)  # another owner's: never a candidate
    save_form4(sessions, TAX_FIX)
    assert links(sessions) == {TAX_FIX: (FILED[TAX_ORIGINAL], None)}
    assert marks(sessions) == {}


@pytest.mark.integration
def test_an_original_accepted_that_evening_and_filed_the_next_day_is_found(
    sessions: sessionmaker[Session],
) -> None:
    # Accepted 2026-02-11 at 6:30 p.m. Eastern, so filed 2026-02-12; the 4/A gives 2026-02-11.
    add_filing(
        sessions,
        TAX_ORIGINAL,
        "4",
        date(2026, 2, 12),
        accepted_at=datetime(2026, 2, 11, 23, 30, tzinfo=UTC),
    )
    save_form4(sessions, TAX_ORIGINAL)
    save_form4(sessions, TAX_FIX)
    assert links(sessions) == {TAX_FIX: (FILED[TAX_ORIGINAL], TAX_ORIGINAL)}


@pytest.mark.integration
def test_a_better_original_arriving_later_takes_the_link_and_the_marks(
    sessions: sessionmaker[Session],
) -> None:
    # First only a look-alike: the owner's Form 4 that day, but with another trade. As the only
    # candidate it is linked, though no line matches.
    look_alike = "0001860253-26-000003"
    data = form4_payload(TAX_ORIGINAL)
    data["transactions"][0]["code"] = "S"
    save_form4(sessions, look_alike, data, FILED[TAX_ORIGINAL])
    save_form4(sessions, TAX_FIX)
    assert links(sessions) == {TAX_FIX: (FILED[TAX_ORIGINAL], look_alike)}
    assert marks(sessions) == {}

    save_form4(sessions, TAX_ORIGINAL)  # shares the corrected line
    assert links(sessions) == {TAX_FIX: (FILED[TAX_ORIGINAL], TAX_ORIGINAL)}
    assert marks(sessions) == {(TAX_ORIGINAL, 1): TAX_FIX}


# --- 8-K/As -------------------------------------------------------------------------------------

APPLE_FIX = "0001140361-26-035325"  # 8-K/A: CEO pay, amends the 8-K "filed on April 20, 2026"
BERKSHIRE_FIX = "0001193125-26-004727"  # 8-K/A giving no date: matched by its report date


def save_8k(
    sessions: sessionmaker[Session],
    accession_no: str,
    filed_at: date,
    items: list[str],
    *,
    report_date: date | None = None,
) -> None:
    add_filing(sessions, accession_no, "8-K", filed_at, report_date=report_date)
    payload = {"items": [{"number": n, "title": f"Item {n}", "text": "..."} for n in items]}
    with sessions.begin() as session:
        form8k.save_key_fields(session, accession_no, payload)


def save_8k_amendment(
    sessions: sessionmaker[Session], accession_no: str, filed_at: date, report_date: date
) -> None:
    add_filing(sessions, accession_no, "8-K/A", filed_at, report_date=report_date)
    payload = form8k.parse_amendment((FORM8K / f"{accession_no}.htm").read_bytes())
    with sessions.begin() as session:
        form8k.save_key_fields(session, accession_no, payload)


@pytest.mark.integration
def test_an_8k_amendment_links_to_the_8k_its_text_names(sessions: sessionmaker[Session]) -> None:
    save_8k(sessions, "0000320193-26-000010", date(2026, 4, 20), ["8.01"])
    save_8k(sessions, "0000320193-26-000011", date(2026, 4, 20), ["5.02", "9.01"])
    save_8k(sessions, "0000320193-26-000012", date(2026, 5, 1), ["5.02"])  # another day
    save_8k_amendment(sessions, APPLE_FIX, date(2026, 9, 1), date(2026, 8, 28))
    # Two 8-Ks that day: the one reporting item 5.02, as the amendment does.
    assert links(sessions) == {APPLE_FIX: (date(2026, 4, 20), "0000320193-26-000011")}


@pytest.mark.integration
def test_an_8k_amendment_without_a_date_links_by_report_date(
    sessions: sessionmaker[Session],
) -> None:
    save_8k_amendment(sessions, BERKSHIRE_FIX, date(2026, 1, 6), date(2025, 5, 3))
    assert links(sessions) == {BERKSHIRE_FIX: (None, None)}  # its 8-K isn't saved yet

    save_8k(
        sessions, "0001193125-25-000020", date(2025, 5, 5), ["5.02"], report_date=date(2025, 5, 3)
    )
    assert links(sessions) == {BERKSHIRE_FIX: (None, "0001193125-25-000020")}


# --- What every stage reads (filings_common.amendments) ----------------------------------------


@pytest.mark.parametrize(
    ("accession_no", "starts"),
    [
        (PSU_FIX, "This amendment is being filed to correct the number of shares acquired"),
        (GIFT_FIX, "Amends the Form 4 filed on September 29, 2025 to correct"),
        (
            OMITTED_SALE,
            "This Amendment to the Form 4 filed on 12/01/2025 is being filed to include",
        ),
        (TAX_FIX, "This amendment to the Form 4 is being filed to report an adjustment"),
        (PRICE_FIX, "The original Form 4 is being amended by this Form 4/A to report the weighted"),
        (POWER_OF_ATTORNEY, "This Amendment on Form 4/A is being filed solely to attach"),
        # In a footnote, after other sentences: only the one naming the mistake.
        (ADDS_TRADES, "The equity swap positions were inadvertently omitted"),
        (OLD_GIFT_FIX, "This filing corrects a reported gift transfer of 11,313 shares"),
    ],
)
def test_the_filers_explanation_of_each_real_amendment(accession_no: str, starts: str) -> None:
    reason = amendment_reason(parse_form4((FORM4 / f"{accession_no}.xml").read_bytes()))
    assert reason is not None
    assert reason.startswith(starts)


@pytest.mark.integration
def test_a_paperwork_amendment_changes_nothing_and_a_correction_does(
    sessions: sessionmaker[Session],
) -> None:
    for accession_no in (
        POWER_OF_ATTORNEY_ORIGINAL,
        POWER_OF_ATTORNEY,
        PRICE_ORIGINAL,
        PRICE_FIX,
        ADDS_TRADES_ORIGINAL,
        ADDS_TRADES,
        OLD_GIFT_FIX,
    ):
        save_form4(sessions, accession_no)
    with sessions() as session:
        assert changes_nothing(session, POWER_OF_ATTORNEY)
        assert not changes_nothing(session, PRICE_FIX)  # 42.265 -> 42.27
        assert not changes_nothing(session, ADDS_TRADES)  # adds 6 lines
        assert not changes_nothing(session, OLD_GIFT_FIX)  # unlinked: nothing to compare
