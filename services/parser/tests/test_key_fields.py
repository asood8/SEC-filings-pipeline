"""Form 4 key fields in Postgres (`form4_filings` and `form4_transactions`), and
`filings-rebuild-key-fields`, which rewrites them from the payloads stored in MongoDB. Uses the
real EDGAR documents in tests/fixtures/form4/."""

import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pymongo.database import Database
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from filings_common.db import (
    Company,
    Filing,
    FilingStatus,
    Form4Filing,
    Form4Transaction,
    OutboxEvent,
    session_factory,
)
from filings_common.mongo import ParsedFilingStore
from filings_parser import form4
from filings_parser.rebuild import main, rebuild_key_fields

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "form4"
FILER_CIK = 1067983  # the watchlist company each test filing was found under

JOINT_PURCHASES = "0001193125-26-397059"  # Berkshire and Buffett buying Lennar: 16 lines
DERIVATIVE_ONLY = "0001284323-26-000002"  # an RSU award, in the derivative table only
EXERCISE_AND_SALE = "0001065280-26-000269"  # stock M and S, then the option's M
FOOTNOTE_PRICE = "0001628280-26-063531"  # the first line's price is only in a footnote
DIRECTOR_AND_CEO = "0001197649-26-000012"  # Jensen Huang: director, "President and CEO"
EVERY_ROLE = "0000950103-26-014647"  # Mark Zuckerberg: director, "COB and CEO", 10% owner

# How form4_filings.reporting_owner_relationships stores an owner who ticked no box.
NO_ROLE = {
    "is_director": False,
    "is_officer": False,
    "is_ten_percent_owner": False,
    "is_other": False,
    "officer_title": None,
    "other_text": None,
}
TEN_PERCENT_OWNER = NO_ROLE | {"is_ten_percent_owner": True}


def payload(accession_no: str) -> dict[str, Any]:
    return form4.parse((FIXTURES / f"{accession_no}.xml").read_bytes())


def lines(session: Session, accession_no: str | None = None) -> list[dict[str, Any]]:
    query = sa.select(Form4Transaction).order_by(
        Form4Transaction.accession_no, Form4Transaction.line
    )
    if accession_no is not None:
        query = query.where(Form4Transaction.accession_no == accession_no)
    return [
        {c.key: getattr(row, c.key) for c in sa.inspect(Form4Transaction).columns}
        for row in session.scalars(query)
    ]


def relationships(sessions: sessionmaker[Session]) -> dict[str, list[dict[str, Any]] | None]:
    """Each saved filing's form4_filings.reporting_owner_relationships."""
    with sessions() as session:
        saved: dict[str, list[dict[str, Any]] | None] = dict(
            session.execute(
                sa.select(Form4Filing.accession_no, Form4Filing.reporting_owner_relationships)
            ).all()
        )
        return saved


def add_filings(
    sessions: sessionmaker[Session],
    accession_nos: list[str],
    status: FilingStatus = FilingStatus.NOTIFIED,
) -> None:
    with sessions.begin() as session:
        if session.get(Company, FILER_CIK) is None:
            session.add(Company(cik=FILER_CIK, ticker="BRK-B", name="BERKSHIRE HATHAWAY INC"))
            session.flush()
        for accession_no in accession_nos:
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=FILER_CIK,
                    form_type="4",
                    filed_at=date(2026, 9, 22),
                    primary_doc_url=f"https://www.sec.gov/x/{accession_no}.xml",
                    raw_object_key=f"filings/{FILER_CIK}/{accession_no}/form4.xml",
                    status=status,
                )
            )


@pytest.fixture
def sessions(clean_db: Engine) -> sessionmaker[Session]:
    return session_factory(clean_db)


def save(sessions: sessionmaker[Session], accession_no: str, data: dict[str, Any]) -> None:
    with sessions.begin() as session:
        form4.save_key_fields(session, accession_no, data)


def test_every_transaction_line_is_saved_in_payload_order(sessions: sessionmaker[Session]) -> None:
    add_filings(sessions, [EXERCISE_AND_SALE])
    save(sessions, EXERCISE_AND_SALE, payload(EXERCISE_AND_SALE))

    with sessions() as session:
        saved = lines(session)
    common = {"accession_no": EXERCISE_AND_SALE, "transaction_date": date(2026, 9, 8)}
    assert saved == [
        common
        | {
            "line": 1,
            "is_derivative": False,
            "security_title": "Common Stock",
            "code": "M",
            "shares": 720,
            "price_per_share": 20.107,
            "acquired_disposed": "A",
            "shares_owned_after": 3180,
            "direct_or_indirect": "D",
        },
        common
        | {
            "line": 2,
            "is_derivative": False,
            "security_title": "Common Stock",
            "code": "S",
            "shares": 720,
            "price_per_share": 77.6,
            "acquired_disposed": "D",
            "shares_owned_after": 2460,
            "direct_or_indirect": "D",
        },
        common
        | {
            "line": 3,
            "is_derivative": True,
            "security_title": "Non-Qualified Stock Option (right to buy)",
            "code": "M",
            "shares": 720,
            "price_per_share": 0,
            "acquired_disposed": "D",
            "shares_owned_after": 230,
            "direct_or_indirect": "D",
        },
    ]


def test_derivative_only_joint_and_footnote_priced_filings(
    sessions: sessionmaker[Session],
) -> None:
    add_filings(sessions, [DERIVATIVE_ONLY, JOINT_PURCHASES, FOOTNOTE_PRICE])
    for accession_no in (DERIVATIVE_ONLY, JOINT_PURCHASES, FOOTNOTE_PRICE):
        save(sessions, accession_no, payload(accession_no))

    with sessions() as session:
        [award] = lines(session, DERIVATIVE_ONLY)
        joint = lines(session, JOINT_PURCHASES)
        footnoted = lines(session, FOOTNOTE_PRICE)
        joint_filing = session.get_one(Form4Filing, JOINT_PURCHASES)
        award_filing = session.get_one(Form4Filing, DERIVATIVE_ONLY)
    assert (award_filing.reporting_owners, award_filing.reporting_owner_ciks) == (
        ["SMITH BRAD D"],
        [1284323],
    )
    assert (award["is_derivative"], award["code"], award["shares"]) == (True, "A", 4086)
    assert award["security_title"] == "Restricted Stock Unit Award"
    # One set of lines however many owners filed it; the owners are on the filing row, their
    # CIKs in the same order as their names.
    assert joint_filing.reporting_owners == ["BERKSHIRE HATHAWAY INC", "BUFFETT WARREN E"]
    assert joint_filing.reporting_owner_ciks == [1067983, 315090]
    assert joint_filing.issuer_cik == 920760  # Lennar, not the filer
    assert [line["line"] for line in joint] == list(range(1, 17))
    assert {line["code"] for line in joint} == {"P"}
    assert {line["direct_or_indirect"] for line in joint} == {"I"}
    assert {line["security_title"] for line in joint} == {
        "Class A Common Stock",
        "Class B Common Stock",
    }
    assert [line["price_per_share"] for line in footnoted] == [None, 103.66, 0]


def test_each_owners_relationship_is_saved_in_owner_order(
    sessions: sessionmaker[Session],
) -> None:
    filings = [JOINT_PURCHASES, DIRECTOR_AND_CEO, EVERY_ROLE, FOOTNOTE_PRICE]
    add_filings(sessions, filings)
    for accession_no in filings:
        save(sessions, accession_no, payload(accession_no))

    assert relationships(sessions) == {
        # Joint filers: Berkshire Hathaway, then Warren Buffett, as in reporting_owners.
        JOINT_PURCHASES: [TEN_PERCENT_OWNER, TEN_PERCENT_OWNER],
        DIRECTOR_AND_CEO: [
            NO_ROLE
            | {"is_director": True, "is_officer": True, "officer_title": "President and CEO"}
        ],
        EVERY_ROLE: [
            NO_ROLE
            | {
                "is_director": True,
                "is_officer": True,
                "is_ten_percent_owner": True,
                "officer_title": "COB and CEO",
            }
        ],
        FOOTNOTE_PRICE: [
            NO_ROLE | {"is_officer": True, "officer_title": "EVP, Control, Fin Plan & Tax"}
        ],
    }


def test_one_relationship_per_owner_is_enforced(sessions: sessionmaker[Session]) -> None:
    add_filings(sessions, [JOINT_PURCHASES])
    save(sessions, JOINT_PURCHASES, payload(JOINT_PURCHASES))
    # One relationship for two owners: a check violation. Not an array: jsonb_array_length fails.
    for wrong, error in (
        ([TEN_PERCENT_OWNER], sa.exc.IntegrityError),
        (TEN_PERCENT_OWNER, sa.exc.DataError),
    ):
        with pytest.raises(error), sessions.begin() as session:
            session.execute(sa.update(Form4Filing).values(reporting_owner_relationships=wrong))


def test_saving_again_leaves_exactly_the_same_rows(sessions: sessionmaker[Session]) -> None:
    add_filings(sessions, [JOINT_PURCHASES])
    save(sessions, JOINT_PURCHASES, payload(JOINT_PURCHASES))
    with sessions() as session:
        first = lines(session)

    save(sessions, JOINT_PURCHASES, payload(JOINT_PURCHASES))  # a replay
    with sessions() as session:
        assert lines(session) == first
        assert session.scalar(sa.select(sa.func.count()).select_from(Form4Filing)) == 1


def test_a_reparse_with_fewer_lines_leaves_none_behind(sessions: sessionmaker[Session]) -> None:
    add_filings(sessions, [EXERCISE_AND_SALE])
    data = payload(EXERCISE_AND_SALE)
    save(sessions, EXERCISE_AND_SALE, data)

    save(sessions, EXERCISE_AND_SALE, data | {"transactions": data["transactions"][:1]})
    with sessions() as session:
        assert [line["line"] for line in lines(session)] == [1]
        assert session.get_one(Form4Filing, EXERCISE_AND_SALE).transaction_codes == ["M"]


def test_deleting_a_filing_deletes_its_lines(sessions: sessionmaker[Session]) -> None:
    add_filings(sessions, [EXERCISE_AND_SALE])
    save(sessions, EXERCISE_AND_SALE, payload(EXERCISE_AND_SALE))
    with sessions.begin() as session:
        session.execute(sa.delete(Filing).where(Filing.accession_no == EXERCISE_AND_SALE))
    with sessions() as session:
        assert lines(session) == []


# --- filings-rebuild-key-fields ------------------------------------------------------------------


def store_payloads(
    mongo_db: Database[dict[str, Any]], accession_nos: list[str], parser_version: int = 1
) -> ParsedFilingStore:
    store = ParsedFilingStore(mongo_db)
    for accession_no in accession_nos:
        store.upsert(
            accession_no=accession_no,
            cik=FILER_CIK,
            form_type="4",
            parser_version=parser_version,
            parsed=payload(accession_no),
        )
    return store


def test_rebuild_fills_the_tables_from_stored_payloads_idempotently(
    sessions: sessionmaker[Session], mongo_db: Database[dict[str, Any]]
) -> None:
    parsed = [JOINT_PURCHASES, DERIVATIVE_ONLY, EXERCISE_AND_SALE]
    add_filings(sessions, parsed)
    store = store_payloads(mongo_db, parsed)
    with sessions() as session:
        updated_at = dict(session.execute(sa.select(Filing.accession_no, Filing.updated_at)).all())

    result = rebuild_key_fields(sessions, store, form_type="4")
    assert (result.rebuilt, result.skipped) == (3, 0)
    with sessions() as session:
        first = lines(session)
        assert session.scalar(sa.select(sa.func.count()).select_from(Form4Filing)) == 3
    assert len(first) == 16 + 1 + 3

    assert rebuild_key_fields(sessions, store).rebuilt == 3  # a second run changes nothing
    with sessions() as session:
        assert lines(session) == first
        # No events, and the filings themselves are untouched.
        assert session.scalars(sa.select(OutboxEvent)).all() == []
        assert dict(session.execute(sa.select(Filing.accession_no, Filing.updated_at)).all()) == (
            updated_at
        )
        assert set(session.scalars(sa.select(Filing.status))) == {FilingStatus.NOTIFIED}


def test_rebuild_fills_owner_ciks_on_rows_saved_before_they_were_stored(
    sessions: sessionmaker[Session], mongo_db: Database[dict[str, Any]]
) -> None:
    parsed = [JOINT_PURCHASES, EXERCISE_AND_SALE]
    add_filings(sessions, parsed)
    for accession_no in parsed:
        save(sessions, accession_no, payload(accession_no))
    with sessions.begin() as session:  # as migration 0046b4a4d18b leaves existing rows
        session.execute(sa.update(Form4Filing).values(reporting_owner_ciks=None))

    assert rebuild_key_fields(sessions, store_payloads(mongo_db, parsed)).rebuilt == 2
    with sessions() as session:
        ciks = dict(
            session.execute(
                sa.select(Form4Filing.accession_no, Form4Filing.reporting_owner_ciks)
            ).all()
        )
    assert ciks == {JOINT_PURCHASES: [1067983, 315090], EXERCISE_AND_SALE: [1184230]}


def test_rebuild_fills_owner_relationships_on_rows_saved_before_they_were_stored(
    sessions: sessionmaker[Session], mongo_db: Database[dict[str, Any]]
) -> None:
    parsed = [JOINT_PURCHASES, EXERCISE_AND_SALE]
    add_filings(sessions, parsed)
    for accession_no in parsed:
        save(sessions, accession_no, payload(accession_no))
    with sessions.begin() as session:  # as migration 1a26f7f8a6f2 leaves existing rows
        session.execute(sa.update(Form4Filing).values(reporting_owner_relationships=None))
    store = store_payloads(mongo_db, parsed)

    assert rebuild_key_fields(sessions, store, form_type="4").rebuilt == 2
    first = relationships(sessions)
    assert first == {
        JOINT_PURCHASES: [TEN_PERCENT_OWNER, TEN_PERCENT_OWNER],
        EXERCISE_AND_SALE: [NO_ROLE | {"is_director": True}],  # Richard Barton, Netflix
    }
    assert rebuild_key_fields(sessions, store, form_type="4").rebuilt == 2  # changes nothing
    assert relationships(sessions) == first


def test_rebuild_skips_unparsed_filings_missing_payloads_and_other_parser_versions(
    sessions: sessionmaker[Session], mongo_db: Database[dict[str, Any]]
) -> None:
    add_filings(sessions, [JOINT_PURCHASES, DERIVATIVE_ONLY])  # no payload for the second
    add_filings(sessions, [FOOTNOTE_PRICE], status=FilingStatus.DISCOVERED)
    add_filings(sessions, [EXERCISE_AND_SALE], status=FilingStatus.PARSED)
    store = store_payloads(mongo_db, [JOINT_PURCHASES, FOOTNOTE_PRICE])
    store_payloads(mongo_db, [EXERCISE_AND_SALE], parser_version=form4.PARSER_VERSION + 1)

    result = rebuild_key_fields(sessions, store)
    assert (result.rebuilt, result.skipped) == (1, 2)
    with sessions() as session:
        assert set(session.scalars(sa.select(Form4Filing.accession_no))) == {JOINT_PURCHASES}

    only = rebuild_key_fields(sessions, store, accession_nos=[JOINT_PURCHASES])
    assert (only.rebuilt, only.skipped) == (1, 0)


def test_rebuild_command(
    clean_db: Engine,
    sessions: sessionmaker[Session],
    mongo_db: Database[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_filings(sessions, [DERIVATIVE_ONLY, EXERCISE_AND_SALE])
    store_payloads(mongo_db, [DERIVATIVE_ONLY])
    monkeypatch.setenv("DATABASE_URL", clean_db.url.render_as_string(hide_password=False))
    monkeypatch.setenv(
        "MONGODB_URL",
        os.environ.get(
            "TEST_MONGODB_URL",
            "mongodb://filings:filings-local-dev@localhost:27017/?authSource=admin",
        ),
    )
    monkeypatch.setenv("MONGODB_DATABASE", mongo_db.name)

    assert main(["--form-type", "4", "--accession", DERIVATIVE_ONLY]) == 0
    assert main(["--form-type", "4"]) == 1  # EXERCISE_AND_SALE has no payload
    with sessions() as session:
        assert [line["accession_no"] for line in lines(session)] == [DERIVATIVE_ONLY]
