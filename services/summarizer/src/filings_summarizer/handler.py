"""Handle one `filings.parsed` event: summarize the filing and announce `filings.ready`.

Safe to run more than once for the same filing:
- the summary is upserted on (filing, provider, model, prompt version), so a redelivery replaces
  its own row, while a replay with a new prompt or model adds a row (the newest is current);
- the status moves parsed → ready with a conditional update, and the `filings.ready` outbox event
  is written in that same transaction only when the update happens. Replays and redeliveries
  therefore never announce a filing twice, which is what keeps alerts from being sent twice. Only
  that first time counts in `filings.ready`.
"""

import logging
from collections.abc import Mapping

import sqlalchemy as sa
from opentelemetry import trace
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from filings_common import metrics, telemetry
from filings_common.consumer import PermanentError, Received
from filings_common.db import Company, Filing, FilingStatus, Summary
from filings_common.embeddings import Embedder, EmbeddingError
from filings_common.events import FilingParsed, FilingReady, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.outbox import enqueue
from filings_common.search import SearchDocument, build_document, upsert_document, upsert_embedding
from filings_summarizer.form4_template import company_name
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.summarizers import Summarizer

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class SummaryHandler:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        parsed: ParsedFilingStore,
        summarizers: Mapping[str, Summarizer],
        embedder: Embedder | None = None,
    ) -> None:
        self._sessions = sessions
        self._parsed = parsed
        self._summarizers = summarizers
        self._embedder = embedder
        metrics.start_at_zero(metrics.FILINGS_READY, map(metrics.form_type, summarizers))

    def __call__(self, received: Received) -> None:
        try:
            event = FilingParsed.model_validate_json(received.value)
        except ValidationError as exc:
            raise PermanentError(f"invalid {Topic.FILINGS_PARSED} event: {exc}") from exc
        telemetry.tag_filing(event.accession_no, form_type=event.form_type, cik=event.cik)

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
        # No transaction is open during the (possibly slow) LLM call. Its errors can quote the
        # model's answer, so the span records them by type only.
        with telemetry.redacted_span(tracer, "summarize") as span:
            result = summarizer.summarize(document["parsed"], context)
            span.set_attributes(
                {
                    "filings.summary.provider": result.provider,
                    "filings.summary.model": result.model,
                    "filings.summary.prompt_version": result.prompt_version,
                }
            )
            if result.ungrounded_numbers is not None:  # a count only: the numbers are LLM text
                span.set_attribute(
                    "filings.summary.ungrounded_numbers", len(result.ungrounded_numbers)
                )
        search_document = build_document(
            result.summary,
            company_name=context.company_name,
            ticker=context.ticker,
            form_type=context.form_type,
        )
        vector = self._embed(search_document, event.accession_no)
        if result.ungrounded_numbers:
            logger.warning(
                "%s summary has numbers not found in the filing: %s",
                event.accession_no,
                ", ".join(result.ungrounded_numbers),
            )

        with self._sessions.begin() as session:
            values = {
                "accession_no": event.accession_no,
                "provider": result.provider,
                "model": result.model,
                "prompt_version": result.prompt_version,
                "summary": result.summary.model_dump(mode="json"),
                "ungrounded_numbers": result.ungrounded_numbers,
            }
            stmt = pg_insert(Summary).values(**values)
            summary_id = session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["accession_no", "provider", "model", "prompt_version"],
                    set_={
                        "summary": stmt.excluded.summary,
                        "ungrounded_numbers": stmt.excluded.ungrounded_numbers,
                        "created_at": sa.func.now(),
                    },
                ).returning(Summary.id)
            ).scalar_one()
            # The newest summary is the current one, so it becomes the search document.
            upsert_document(session, event.accession_no, summary_id, search_document)
            if vector is not None and self._embedder is not None:
                upsert_embedding(session, event.accession_no, self._embedder.model, vector)
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
        metrics.FILINGS_READY.add(1, metrics.form_type(event.form_type))
        logger.info("summarized %s with %s/%s", event.accession_no, result.provider, result.model)

    def _embed(self, document: SearchDocument, accession_no: str) -> list[float] | None:
        """The document's embedding, or None if embeddings are off or the embedder fails.

        Never fatal: the filing stays keyword-searchable, and `filings-index` embeds it later.
        """
        if self._embedder is None:
            return None
        try:
            return self._embedder.embed_documents([document.text()])[0]
        except EmbeddingError as exc:
            logger.warning("not embedding %s now: %s", accession_no, exc)
            return None
