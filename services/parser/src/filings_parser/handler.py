"""Handle one `filings.discovered` event: parse the raw filing and announce `filings.parsed`.

Safe to run more than once for the same filing (the consume loop delivers at least once):
- the parsed payload (MongoDB) and key fields (Postgres) are upserts keyed by accession number;
- the status moves discovered → parsed with a conditional update, and the `filings.parsed` outbox
  event is written in that same transaction only when the update happens, so a redelivery never
  announces the filing twice.
"""

import logging
from collections.abc import Mapping

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from filings_common.consumer import PermanentError, Received
from filings_common.db import Filing, FilingExhibit, FilingStatus
from filings_common.events import FilingDiscovered, FilingParsed, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.outbox import enqueue
from filings_common.storage import ObjectNotFoundError, ObjectStore
from filings_parser.documents import RawExhibit
from filings_parser.forms import FormParser

logger = logging.getLogger(__name__)


class FilingHandler:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        objects: ObjectStore,
        parsed: ParsedFilingStore,
        parsers: Mapping[str, FormParser],
    ) -> None:
        self._sessions = sessions
        self._objects = objects
        self._parsed = parsed
        self._parsers = parsers

    def __call__(self, received: Received) -> None:
        try:
            event = FilingDiscovered.model_validate_json(received.value)
        except ValidationError as exc:
            raise PermanentError(f"invalid {Topic.FILINGS_DISCOVERED} event: {exc}") from exc

        parser = self._parsers.get(event.form_type)
        if parser is None:
            logger.info(
                "no parser for form %s yet; %s stays discovered",
                event.form_type,
                event.accession_no,
            )
            return
        try:
            raw = self._objects.get(event.raw_object_key)
        except ObjectNotFoundError as exc:
            raise PermanentError(f"raw document missing: {event.raw_object_key}") from exc
        exhibits = self._exhibits(event.accession_no)
        try:
            payload = parser.parse(raw, exhibits)
        except Exception as exc:
            # Parsing is deterministic: the same document fails the same way on every retry.
            raise PermanentError(f"form {event.form_type} parser failed: {exc!r}") from exc

        self._parsed.upsert(
            accession_no=event.accession_no,
            cik=event.cik,
            form_type=event.form_type,
            parser_version=parser.version,
            parsed=payload,
        )
        with self._sessions.begin() as session:
            moved = session.execute(
                sa.update(Filing)
                .where(
                    Filing.accession_no == event.accession_no,
                    Filing.status == FilingStatus.DISCOVERED,
                )
                .values(status=FilingStatus.PARSED, updated_at=sa.func.now())
                .returning(Filing.accession_no)
            ).scalar_one_or_none()
            if moved is None and session.get(Filing, event.accession_no) is None:
                raise PermanentError(f"no filing row for {event.accession_no}")
            if parser.save_key_fields is not None:  # upserts, so always safe to rewrite
                parser.save_key_fields(session, event.accession_no, payload)
            if moved is None:
                logger.info("%s was already parsed; not announcing it again", event.accession_no)
                return
            enqueue(
                session,
                Topic.FILINGS_PARSED,
                FilingParsed(
                    accession_no=event.accession_no,
                    cik=event.cik,
                    form_type=event.form_type,
                    parser_version=parser.version,
                ),
            )
        logger.info("parsed %s (form %s)", event.accession_no, event.form_type)

    def _exhibits(self, accession_no: str) -> list[RawExhibit]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(FilingExhibit)
                .where(FilingExhibit.accession_no == accession_no)
                .order_by(FilingExhibit.sequence)
            ).all()
        exhibits = []
        for row in rows:
            try:
                content = self._objects.get(row.object_key)
            except ObjectNotFoundError:
                logger.warning(
                    "exhibit %s of %s is missing; parsing without it", row.filename, accession_no
                )
                continue
            exhibits.append(RawExhibit(row.exhibit_type, row.filename, row.description, content))
        return exhibits
