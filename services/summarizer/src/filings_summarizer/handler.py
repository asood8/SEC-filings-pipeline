"""Handle one `filings.parsed` event: summarize the filing and announce `filings.ready`.

Safe to run more than once for the same filing:
- the summary is upserted on (filing, provider, model, prompt version), so a redelivery replaces
  its own row, while a replay with a new prompt or model adds a row (the newest is current);
- the status moves parsed → ready with a conditional update, and the `filings.ready` outbox event
  is written in that same transaction only when the update happens. Replays and redeliveries
  therefore never announce a filing twice, which is what keeps alerts from being sent twice.
"""

import logging
from collections.abc import Mapping

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from filings_common.consumer import PermanentError, Received
from filings_common.db import Company, Filing, FilingStatus, Summary
from filings_common.events import FilingParsed, FilingReady, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.outbox import enqueue
from filings_summarizer.form4_template import company_name
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.summarizers import Summarizer

logger = logging.getLogger(__name__)


class SummaryHandler:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        parsed: ParsedFilingStore,
        summarizers: Mapping[str, Summarizer],
    ) -> None:
        self._sessions = sessions
        self._parsed = parsed
        self._summarizers = summarizers

    def __call__(self, received: Received) -> None:
        try:
            event = FilingParsed.model_validate_json(received.value)
        except ValidationError as exc:
            raise PermanentError(f"invalid {Topic.FILINGS_PARSED} event: {exc}") from exc

        summarizer = self._summarizers.get(event.form_type)
        if summarizer is None:
            logger.info(
                "no summarizer for form %s yet; %s stays parsed",
                event.form_type,
                event.accession_no,
            )
            return
        document = self._parsed.get(event.accession_no)
        if document is None:
            raise PermanentError(f"no parsed payload for {event.accession_no}")
        with self._sessions() as session:
            row = session.execute(
                sa.select(Filing, Company)
                .join(Company, Company.cik == Filing.cik)
                .where(Filing.accession_no == event.accession_no)
            ).one_or_none()
        if row is None:
            raise PermanentError(f"no filing row for {event.accession_no}")
        filing, company = row
        context = FilingContext(
            accession_no=filing.accession_no,
            form_type=filing.form_type,
            company_name=company_name(company.name),
            ticker=company.ticker,
            filed_at=filing.filed_at,
        )
        # No transaction is open during the (possibly slow) LLM call.
        result = summarizer.summarize(document["parsed"], context)

        with self._sessions.begin() as session:
            values = {
                "accession_no": event.accession_no,
                "provider": result.provider,
                "model": result.model,
                "prompt_version": result.prompt_version,
                "summary": result.summary.model_dump(mode="json"),
            }
            stmt = pg_insert(Summary).values(**values)
            summary_id = session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["accession_no", "provider", "model", "prompt_version"],
                    set_={"summary": stmt.excluded.summary, "created_at": sa.func.now()},
                ).returning(Summary.id)
            ).scalar_one()
            moved = session.execute(
                sa.update(Filing)
                .where(
                    Filing.accession_no == event.accession_no,
                    Filing.status == FilingStatus.PARSED,
                )
                .values(status=FilingStatus.READY, updated_at=sa.func.now())
                .returning(Filing.accession_no)
            ).scalar_one_or_none()
            if moved is None:
                logger.info("%s was already ready; summary updated, not announced again", event.key)
                return
            enqueue(
                session,
                Topic.FILINGS_READY,
                FilingReady(
                    accession_no=event.accession_no,
                    cik=event.cik,
                    form_type=event.form_type,
                    summary_id=summary_id,
                ),
            )
        logger.info("summarized %s with %s/%s", event.accession_no, result.provider, result.model)
