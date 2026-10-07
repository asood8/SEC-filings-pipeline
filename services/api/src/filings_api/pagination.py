"""Keyset (cursor) pagination: a cursor encodes the sort key of the last item on a page.

Unlike offsets, cursors stay correct while new filings arrive: a page never repeats or skips rows
because something was inserted in front of it.
"""

import base64
import binascii
import json
from typing import Any

from fastapi import HTTPException


def encode_cursor(*values: Any) -> str:
    raw = json.dumps(list(values), default=str, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, size: int) -> list[Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    if not isinstance(values, list) or len(values) != size:
        raise HTTPException(status_code=400, detail="invalid cursor")
    return values
