"""`filings-index`: rebuild the search index from current summaries and embed what's missing.

Run it after enabling or changing the embedding model, or to backfill filings summarized before
search existed. It is idempotent. New summaries are indexed by the summarizer as they're written.
"""

import logging

from filings_common.config import DatabaseSettings, EmbeddingSettings
from filings_common.db import create_engine, session_factory
from filings_common.embeddings import embedder_from_settings
from filings_common.search import rebuild_index

logger = logging.getLogger("filings_index")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    embedder = embedder_from_settings(EmbeddingSettings())
    try:
        documents, embedded = rebuild_index(session_factory(engine), embedder)
    finally:
        engine.dispose()
    model = embedder.model if embedder else "none (EMBEDDING_MODEL unset)"
    logger.info("indexed %d filings; embedded %d with %s", documents, embedded, model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
