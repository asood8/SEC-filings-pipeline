"""Form parsers, by EDGAR form type.

Each parser turns a raw document into a JSON-ready dict, stored in MongoDB. It can also write a
few queryable key fields to Postgres. Bump `version` whenever a parser's output changes; it is
stored with every parsed filing so outdated ones can be found and reparsed.

Forms without a parser are acknowledged and stay `discovered`, ready to be reparsed once one
exists (`filings-replay discovered`).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from filings_parser import form4, form8k
from filings_parser.documents import RawExhibit


@dataclass(frozen=True)
class FormParser:
    version: int
    # The primary document and the filing's collected exhibits, to a JSON-ready payload.
    parse: Callable[[bytes, Sequence[RawExhibit]], dict[str, Any]]
    # Upserts the form's key fields into Postgres, inside the handler's transaction.
    save_key_fields: Callable[[Session, str, dict[str, Any]], None] | None = None


PARSERS: Mapping[str, FormParser] = {
    "4": FormParser(
        version=form4.PARSER_VERSION, parse=form4.parse, save_key_fields=form4.save_key_fields
    ),
    "8-K": FormParser(
        version=form8k.PARSER_VERSION, parse=form8k.parse, save_key_fields=form8k.save_key_fields
    ),
}
