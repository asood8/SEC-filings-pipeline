"""Parsed filings in MongoDB (Atlas in production), one document per accession number, and the
earnings-call transcripts companies publish (ADR-0022), one document per source URL.

The natural key is the document `_id`, so writes are naturally idempotent upserts. Payload
shapes differ by form type, which is why they live in a document store rather than Postgres.

Both are working copies: MongoDB deletes each document a retention period after it was written
(a TTL index; DocumentRetentionSettings), which keeps Atlas's free 512 MB from filling up.
"""

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind
from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

COLLECTION = "parsed_filings"
TRANSCRIPTS = "call_transcripts"

tracer = trace.get_tracer(__name__)


def _span(collection: Collection[dict[str, Any]], operation: str) -> AbstractContextManager[Span]:
    return tracer.start_as_current_span(
        f"{operation} {collection.name}",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system.name": "mongodb",
            "db.namespace": collection.database.name,
            "db.collection.name": collection.name,
            "db.operation.name": operation,
        },
    )


def expire_after(collection: Collection[dict[str, Any]], field: str, retention: timedelta) -> None:
    """Have MongoDB delete each document `retention` after the time in its `field` (a TTL index,
    named `<field>_ttl`; MongoDB's TTL monitor deletes expired documents about once a minute).
    Idempotent; a changed retention updates the index in place (collMod)."""
    name = f"{field}_ttl"
    seconds = int(retention.total_seconds())
    current = collection.index_information().get(name)
    if current is None:
        collection.create_index([(field, ASCENDING)], name=name, expireAfterSeconds=seconds)
    elif current.get("expireAfterSeconds") != seconds:
        collection.database.command(
            "collMod", collection.name, index={"name": name, "expireAfterSeconds": seconds}
        )


class ParsedFilingStore:
    def __init__(self, database: Database[dict[str, Any]]) -> None:
        self._collection = database[COLLECTION]

    def _span(self, operation: str) -> AbstractContextManager[Span]:
        return _span(self._collection, operation)

    def ensure_indexes(self, retention: timedelta) -> None:
        """The lookup index, and expiry `retention` after each document was last parsed."""
        self._collection.create_index([("cik", 1), ("form_type", 1)])
        expire_after(self._collection, "parsed_at", retention)

    def upsert(
        self,
        *,
        accession_no: str,
        cik: int,
        form_type: str,
        parser_version: int,
        parsed: dict[str, Any],
    ) -> None:
        with self._span("replace_one"):
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
        with self._span("find_one"):
            document = self._collection.find_one({"_id": accession_no})
        if document is None:
            return None
        document["accession_no"] = document.pop("_id")
        return document

    def get_many(self, accession_nos: list[str]) -> dict[str, dict[str, Any]]:
        """The stored documents among `accession_nos`, shaped like `get`'s, by accession number
        (missing ones omitted)."""
        with self._span("find"):
            documents = list(self._collection.find({"_id": {"$in": accession_nos}}))
        for document in documents:
            document["accession_no"] = document.pop("_id")
        return {document["accession_no"]: document for document in documents}

    def parser_versions(self, accession_nos: list[str]) -> dict[str, int]:
        """The parser version of each stored filing among `accession_nos` (missing ones omitted)."""
        cursor = self._collection.find(
            {"_id": {"$in": accession_nos}}, {"_id": 1, "parser_version": 1}
        )
        return {doc["_id"]: doc["parser_version"] for doc in cursor}


class TranscriptStore:
    """The text of each stored earnings-call transcript, by source URL. Never served: the API
    serves only summaries of it (ADR-0022)."""

    def __init__(self, database: Database[dict[str, Any]]) -> None:
        self._collection = database[TRANSCRIPTS]

    def ensure_indexes(self, retention: timedelta) -> None:
        """Expiry `retention` after each transcript was stored."""
        expire_after(self._collection, "stored_at", retention)

    def upsert(self, *, source_url: str, cik: int, ticker: str, call_date: date, text: str) -> None:
        with _span(self._collection, "replace_one"):
            self._collection.replace_one(
                {"_id": source_url},
                {
                    "cik": cik,
                    "ticker": ticker,
                    "call_date": call_date.isoformat(),
                    "text": text,
                    "stored_at": datetime.now(UTC),
                },
                upsert=True,
            )

    def text(self, source_url: str) -> str | None:
        with _span(self._collection, "find_one"):
            document = self._collection.find_one({"_id": source_url}, {"text": 1})
        return None if document is None else str(document["text"])
