"""Form parsers, by EDGAR form type.

Each parser turns a raw document into a JSON-ready dict, stored in MongoDB. It can also write a
few queryable key fields to Postgres. Bump `version` whenever a parser's output changes; it is
stored with every parsed filing so outdated ones can be found and reparsed.

Forms without a parser (8-K until Phase 2) are acknowledged and stay `discovered`, ready to be
reparsed once one exists.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from filings_parser import form4


@dataclass(frozen=True)
class FormParser:
    version: int
    parse: Callable[[bytes], dict[str, Any]]
    # Upserts the form's key fields into Postgres, inside the handler's transaction.
    save_key_fields: Callable[[Session, str, dict[str, Any]], None] | None = None


PARSERS: Mapping[str, FormParser] = {
    "4": FormParser(
        version=form4.PARSER_VERSION, parse=form4.parse, save_key_fields=form4.save_key_fields
    ),
}
