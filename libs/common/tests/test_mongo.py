from typing import Any

import pytest
from pymongo.database import Database

from filings_common.mongo import ParsedFilingStore

pytestmark = pytest.mark.integration

ACCESSION = "0001140361-26-000202"


def test_upsert_is_idempotent_and_replaces_the_payload(mongo_db: Database[dict[str, Any]]) -> None:
    store = ParsedFilingStore(mongo_db)
    store.ensure_indexes()
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
