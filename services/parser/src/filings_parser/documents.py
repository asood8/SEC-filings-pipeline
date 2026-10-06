"""A filing's raw documents as handed to a form parser."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RawExhibit:
    type: str  # e.g. "EX-99.1"
    filename: str
    description: str | None
    content: bytes
