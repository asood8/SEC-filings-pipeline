"""Parsed filings in MongoDB (Atlas in production), one document per accession number.

The accession number is the document `_id`, so writes are naturally idempotent upserts. Payload
shapes differ by form type, which is why they live in a document store rather than Postgres.
"""

from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

COLLECTION = "parsed_filings"


class ParsedFilingStore:
    def __init__(self, database: Database[dict[str, Any]]) -> None:
        self._collection = database[COLLECTION]

    def ensure_indexes(self) -> None:
        self._collection.create_index([("cik", 1), ("form_type", 1)])

    def upsert(
        self,
        *,
        accession_no: str,
        cik: int,
        form_type: str,
        parser_version: int,
        parsed: dict[str, Any],
    ) -> None:
        self._collection.replace_one(
            {"_id": accession_no},
            {
                "cik": cik,
                "form_type": form_type,
                "parser_version": parser_version,
                "parsed": parsed,
                "parsed_at": datetime.now(UTC),
            },
            upsert=True,
        )

    def get(self, accession_no: str) -> dict[str, Any] | None:
        document = self._collection.find_one({"_id": accession_no})
        if document is None:
            return None
        document["accession_no"] = document.pop("_id")
        return document

    def parser_versions(self, accession_nos: list[str]) -> dict[str, int]:
        """The parser version of each stored filing among `accession_nos` (missing ones omitted)."""
        cursor = self._collection.find(
            {"_id": {"$in": accession_nos}}, {"_id": 1, "parser_version": 1}
        )
        return {doc["_id"]: doc["parser_version"] for doc in cursor}
