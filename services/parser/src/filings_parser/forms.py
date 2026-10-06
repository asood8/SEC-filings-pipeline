"""Form parsers, by EDGAR form type.

Each parser turns a raw document into a JSON-serializable dict. Bump `version` whenever a parser's
output changes; it is stored with every parsed filing so outdated ones can be found and reparsed.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FormParser:
    version: int
    parse: Callable[[bytes], dict[str, Any]]


# The Form 4 parser is added together with real EDGAR documents as test fixtures (tests/fixtures/).
# Until then, every filing is dead-lettered as unsupported.
PARSERS: Mapping[str, FormParser] = {}
