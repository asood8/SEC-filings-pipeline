from datetime import timedelta
from typing import Any

import pytest
from pymongo.database import Database

from filings_common.mongo import ParsedFilingStore, TranscriptStore

pytestmark = pytest.mark.integration

ACCESSION = "0001140361-26-000202"


def test_upsert_is_idempotent_and_replaces_the_payload(mongo_db: Database[dict[str, Any]]) -> None:
    store = ParsedFilingStore(mongo_db)
    store.ensure_indexes(timedelta(days=365))
    for version in (1, 2):
        store.upsert(
            accession_no=ACCESSION,
            cik=320193,
            form_type="4",
            parser_version=version,
            parsed={"a": version},
        )

    assert mongo_db["parsed_filings"].count_documents({}) == 1
    document = store.get(ACCESSION)
    assert document is not None
    assert document["accession_no"] == ACCESSION
    assert (document["parser_version"], document["parsed"]) == (2, {"a": 2})


def test_get_missing_returns_none(mongo_db: Database[dict[str, Any]]) -> None:
    assert ParsedFilingStore(mongo_db).get(ACCESSION) is None


def test_get_many_returns_the_stored_documents_by_accession(
    mongo_db: Database[dict[str, Any]],
) -> None:
    store = ParsedFilingStore(mongo_db)
    for accession_no in (ACCESSION, "0001140361-26-000203"):
        store.upsert(
            accession_no=accession_no,
            cik=320193,
            form_type="4",
            parser_version=1,
            parsed={"a": accession_no},
        )

    documents = store.get_many([ACCESSION, "0000000000-26-000001"])
    assert list(documents) == [ACCESSION]  # missing ones omitted, others not asked for
    assert documents[ACCESSION]["accession_no"] == ACCESSION
    assert (documents[ACCESSION]["parser_version"], documents[ACCESSION]["parsed"]) == (
        1,
        {"a": ACCESSION},
    )
    assert store.get_many([]) == {}


def test_documents_expire_after_the_retention(mongo_db: Database[dict[str, Any]]) -> None:
    year = 365 * 86400
    ParsedFilingStore(mongo_db).ensure_indexes(timedelta(days=365))
    TranscriptStore(mongo_db).ensure_indexes(timedelta(days=365))
    for collection, field in (("parsed_filings", "parsed_at"), ("call_transcripts", "stored_at")):
        index = mongo_db[collection].index_information()[f"{field}_ttl"]
        assert (index["key"], index["expireAfterSeconds"]) == ([(field, 1)], year)


def test_a_changed_retention_updates_the_expiry_in_place(
    mongo_db: Database[dict[str, Any]],
) -> None:
    store = ParsedFilingStore(mongo_db)
    store.ensure_indexes(timedelta(days=365))
    store.ensure_indexes(timedelta(days=365))  # unchanged: nothing to do
    store.ensure_indexes(timedelta(days=90))
    indexes = mongo_db["parsed_filings"].index_information()
    assert indexes["parsed_at_ttl"]["expireAfterSeconds"] == 90 * 86400
    assert sorted(indexes) == ["_id_", "cik_1_form_type_1", "parsed_at_ttl"]
