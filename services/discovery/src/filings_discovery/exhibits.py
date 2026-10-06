"""Find and download a filing's EX-99 exhibits (press releases, investor materials).

The filing folder's file list doesn't say which file is which exhibit. EDGAR's
`<accession>-index-headers.html` does: it holds the SGML header of every document in the
filing (`<TYPE>EX-99.1`, `<FILENAME>...`). Only EX-99 exhibits in HTML or text are kept; contracts
(EX-10), XBRL files (EX-101), and images are not worth summarizing.
"""

import html
import re
from dataclasses import dataclass

import httpx2
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from filings_common.db import FilingExhibit
from filings_common.edgar import EdgarClient, archive_url
from filings_common.storage import ObjectStore

_EX99 = re.compile(r"^EX-99(\.\d+)*[A-Z]?$", re.IGNORECASE)
_TEXT_FILE = re.compile(r"\.(htm|html|txt)$", re.IGNORECASE)
_DOCUMENT = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.DOTALL)


@dataclass(frozen=True)
class DocumentRef:
    type: str
    sequence: int
    filename: str
    description: str | None


@dataclass(frozen=True)
class DownloadedExhibit:
    ref: DocumentRef
    object_key: str


def index_headers_url(cik: int, accession_no: str) -> str:
    return archive_url(cik, accession_no, f"{accession_no}-index-headers.html")


def parse_index_headers(page: str) -> list[DocumentRef]:
    """The documents listed in an index-headers page, in filing order."""
    # The page has the filing header in an HTML comment, then each document's header as escaped
    # SGML inside <PRE>. Drop comments and HTML tags, then unescape the SGML.
    visible = re.sub(r"<!--.*?-->", "", page, flags=re.DOTALL)
    sgml = html.unescape(re.sub(r"<[^>]+>", "", visible))
    refs = []
    for block in _DOCUMENT.findall(sgml):
        fields = dict(re.findall(r"^<([A-Z]+)>(.*)$", block, flags=re.MULTILINE))
        if "TYPE" in fields and "FILENAME" in fields:
            refs.append(
                DocumentRef(
                    type=fields["TYPE"].strip(),
                    sequence=int(fields.get("SEQUENCE", "0").strip() or 0),
                    filename=fields["FILENAME"].strip(),
                    description=(fields.get("DESCRIPTION") or "").strip() or None,
                )
            )
    return refs


def ex99_exhibits(documents: list[DocumentRef]) -> list[DocumentRef]:
    return [d for d in documents if _EX99.match(d.type) and _TEXT_FILE.search(d.filename)]


def download_exhibits(
    edgar: EdgarClient, store: ObjectStore, cik: int, accession_no: str, key_prefix: str
) -> list[DownloadedExhibit]:
    """Fetch the filing's index headers, then store each EX-99 exhibit under `key_prefix`."""
    try:
        page = edgar.get(index_headers_url(cik, accession_no)).text
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code == 404:  # very old filings have no index-headers page
            return []
        raise
    downloaded = []
    for ref in ex99_exhibits(parse_index_headers(page)):
        response = edgar.get(archive_url(cik, accession_no, ref.filename))
        key = f"{key_prefix}/{ref.filename}"
        store.put(key, response.content, content_type=response.headers.get("Content-Type"))
        downloaded.append(DownloadedExhibit(ref, key))
    return downloaded


def record_exhibits(session: Session, accession_no: str, exhibits: list[DownloadedExhibit]) -> None:
    """Insert exhibit rows (idempotent)."""
    if not exhibits:
        return
    session.execute(
        pg_insert(FilingExhibit)
        .values(
            [
                {
                    "accession_no": accession_no,
                    "filename": e.ref.filename,
                    "exhibit_type": e.ref.type.upper(),
                    "sequence": e.ref.sequence,
                    "description": e.ref.description,
                    "object_key": e.object_key,
                }
                for e in exhibits
            ]
        )
        .on_conflict_do_nothing(index_elements=["accession_no", "filename"])
    )


def has_exhibit_rows(session: Session, accession_no: str) -> bool:
    query = sa.select(sa.exists().where(FilingExhibit.accession_no == accession_no))
    return bool(session.scalar(query))
