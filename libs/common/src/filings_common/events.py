"""Events exchanged between pipeline stages.

Events carry IDs and pointers, never full documents. Every event is keyed by accession number and
has a `schema_version` so consumers can handle old messages during replay.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

# EDGAR accession number, e.g. 0000320193-24-000123 (filer agent CIK, year, sequence).
AccessionNo = Annotated[str, StringConstraints(pattern=r"^\d{10}-\d{2}-\d{6}$")]


class Topic(StrEnum):
    FILINGS_DISCOVERED = "filings.discovered"
    FILINGS_PARSED = "filings.parsed"
    FILINGS_READY = "filings.ready"

    @property
    def dlq(self) -> str:
        return f"{self.value}.dlq"


class Event(BaseModel):
    # Unknown fields are ignored so older consumers tolerate newer producers.
    model_config = ConfigDict(frozen=True)

    accession_no: AccessionNo

    @property
    def key(self) -> str:
        return self.accession_no


class FilingDiscovered(Event):
    """A new filing's raw document is stored and its row exists in Postgres."""

    schema_version: Literal[1] = 1
    cik: int
    form_type: str
    filed_at: date
    raw_object_key: str
