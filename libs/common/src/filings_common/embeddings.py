"""Text embeddings for semantic search, behind an interface so the model or server can change.

Vectors from different models can't be compared, so there is no failover between embedders: one
configured model embeds both the documents and the queries. When it is unavailable, callers
degrade (search falls back to keywords, documents are embedded later by `filings-index`).
"""

from collections.abc import Sequence
from typing import Protocol

import httpx2
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from filings_common import telemetry
from filings_common.config import EmbeddingSettings
from filings_common.db import EMBEDDING_DIMENSIONS

tracer = trace.get_tracer(__name__)


class EmbeddingError(Exception):
    pass


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OpenAICompatibleEmbedder:
    """Calls the `/embeddings` endpoint that Ollama, llama.cpp, and OpenAI all serve."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        document_prefix: str = "",
        query_prefix: str = "",
        timeout: float = 60.0,
        dimensions: int = EMBEDDING_DIMENSIONS,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._document_prefix = document_prefix
        self._query_prefix = query_prefix
        self._dimensions = dimensions
        self._http = httpx2.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self._document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_prefix + text])[0]

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        # The span never holds the texts (search queries are user input); nor do its errors.
        with telemetry.redacted_span(
            tracer,
            f"embeddings {self.model}",
            kind=SpanKind.CLIENT,
            attributes={
                telemetry.GEN_AI_OPERATION: "embeddings",
                telemetry.GEN_AI_MODEL: self.model,
                "filings.embedding.inputs": len(inputs),
            },
        ):
            return self._request(inputs)

    def _request(self, inputs: list[str]) -> list[list[float]]:
        try:
            response = self._http.post("/embeddings", json={"model": self.model, "input": inputs})
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in d["embedding"]] for d in data]
        except (httpx2.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"embedding with {self.model} failed: {exc!r}") from exc
        if len(vectors) != len(inputs) or any(len(v) != self._dimensions for v in vectors):
            raise EmbeddingError(
                f"{self.model} returned {len(vectors)} vectors of sizes "
                f"{sorted({len(v) for v in vectors})}; expected {len(inputs)} of {self._dimensions}"
            )
        return vectors


def embedder_from_settings(settings: EmbeddingSettings) -> Embedder | None:
    if not settings.model:
        return None
    return OpenAICompatibleEmbedder(
        base_url=settings.base_url,
        model=settings.model,
        document_prefix=settings.document_prefix,
        query_prefix=settings.query_prefix,
        timeout=settings.timeout_seconds,
    )
