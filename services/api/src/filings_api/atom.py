"""A minimal Atom writer (RFC 4287) for the filings feed (`GET /feeds/filings.atom`).

Only what the feed needs: entries with an HTML body, a link, an author, and one category. The XML
is built with ElementTree, which escapes text and attributes; characters XML 1.0 can't hold (most
control characters, lone surrogates) are removed first, since a summary is model output.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

ATOM_NS = "http://www.w3.org/2005/Atom"
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    published: datetime
    updated: datetime
    link: str
    author: str
    category: str
    html: str  # the body as HTML; escaped again here, as Atom's type="html" expects


def _text(text: str) -> str:
    return _NOT_XML.sub("", text)


def _time(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")  # RFC 3339; the columns are time zone aware


def _add(parent: ET.Element, tag: str, text: str | None = None, **attributes: str) -> ET.Element:
    element = ET.SubElement(parent, tag, {k: _text(v) for k, v in attributes.items()})
    if text is not None:
        element.text = _text(text)
    return element


def feed(
    entries: Iterable[Entry], *, id: str, title: str, self_url: str, updated: datetime
) -> bytes:
    root = ET.Element("feed", {"xmlns": ATOM_NS})
    _add(root, "id", id)
    _add(root, "title", title)
    _add(root, "updated", _time(updated))
    _add(root, "link", rel="self", type="application/atom+xml", href=self_url)
    _add(root, "generator", "SEC filings pipeline")
    for entry in entries:
        element = _add(root, "entry")
        _add(element, "id", entry.id)
        _add(element, "title", entry.title)
        _add(element, "published", _time(entry.published))
        _add(element, "updated", _time(entry.updated))
        _add(element, "link", rel="alternate", type="text/html", href=entry.link)
        _add(_add(element, "author"), "name", entry.author)
        _add(element, "category", term=entry.category)
        _add(element, "content", entry.html, type="html")
    body: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return body
