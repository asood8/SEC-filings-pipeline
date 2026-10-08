"""`filings-index`: rebuild the search index from current summaries and embed what's missing.

Run it after enabling or changing the embedding model, after changing how search documents are
built (their fields, or the keyword lists in filings_common.search), or to backfill filings
summarized before search existed. It is idempotent, and it re-embeds nothing that has an
embedding. New summaries are indexed by the summarizer as they're written.
"""

import logging

from opentelemetry import trace

from filings_common.config import DatabaseSettings, EmbeddingSettings
from filings_common.db import create_engine, session_factory
from filings_common.embeddings import embedder_from_settings
from filings_common.search import rebuild_index
from filings_common.telemetry import setup_telemetry

logger = logging.getLogger("filings_index")
tracer = trace.get_tracer(__name__)


def main() -> int:
    telemetry = setup_telemetry("filings-index", distribution="filings-common")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    embedder = embedder_from_settings(EmbeddingSettings())
    try:
        with tracer.start_as_current_span("rebuild index"):
            documents, embedded = rebuild_index(session_factory(engine), embedder)
    finally:
        engine.dispose()
        telemetry.shutdown()
    model = embedder.model if embedder else "none (EMBEDDING_MODEL unset)"
    logger.info("indexed %d filings; embedded %d with %s", documents, embedded, model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
