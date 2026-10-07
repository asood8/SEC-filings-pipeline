import json

import httpx2
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from filings_common.config import EmbeddingSettings
from filings_common.embeddings import (
    EmbeddingError,
    OpenAICompatibleEmbedder,
    embedder_from_settings,
)


def embedder(
    response: httpx2.Response | Exception, requests: list[httpx2.Request], dimensions: int = 3
) -> OpenAICompatibleEmbedder:
    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    return OpenAICompatibleEmbedder(
        base_url="http://llm:11434/v1/",
        model="embed-test",
        document_prefix="doc: ",
        query_prefix="query: ",
        dimensions=dimensions,
        transport=httpx2.MockTransport(handler),
    )


def vectors(*rows: list[float]) -> httpx2.Response:
    # Out of order on purpose: results are matched to inputs by index.
    data = [{"index": i, "embedding": row} for i, row in reversed(list(enumerate(rows)))]
    return httpx2.Response(200, json={"data": data})


def test_documents_and_queries_get_their_task_prefixes() -> None:
    requests: list[httpx2.Request] = []
    e = embedder(vectors([1, 0, 0], [0, 1, 0]), requests)
    assert e.embed_documents(["first", "second"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    body = json.loads(requests[0].content)
    assert str(requests[0].url) == "http://llm:11434/v1/embeddings"
    assert body == {"model": "embed-test", "input": ["doc: first", "doc: second"]}

    e = embedder(vectors([0, 0, 1]), requests)
    assert e.embed_query("earnings") == [0.0, 0.0, 1.0]
    assert json.loads(requests[1].content)["input"] == ["query: earnings"]


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(500),
        httpx2.Response(200, json={"unexpected": True}),
        httpx2.ConnectError("connection refused"),
        vectors([1, 0]),  # wrong dimension
    ],
)
def test_failures_raise_embedding_error(response: httpx2.Response | Exception) -> None:
    with pytest.raises(EmbeddingError):
        embedder(response, []).embed_documents(["text"])


def test_no_request_for_no_texts() -> None:
    requests: list[httpx2.Request] = []
    assert embedder(vectors(), requests).embed_documents([]) == []
    assert requests == []


def test_settings_enable_embeddings_only_with_a_model() -> None:
    assert embedder_from_settings(EmbeddingSettings(_env_file=None, model=None)) is None
    configured = embedder_from_settings(EmbeddingSettings(_env_file=None, model="nomic"))
    assert configured is not None
    assert configured.model == "nomic"


def test_embedding_calls_are_spans_without_the_texts(spans: InMemorySpanExporter) -> None:
    requests: list[httpx2.Request] = []
    embedder(vectors([1.0, 0.0, 0.0]), requests).embed_query("insider selling at acme")
    with pytest.raises(EmbeddingError):  # an error that could quote the input
        embedder(ValueError("bad input: insider selling at acme"), requests).embed_documents(
            ["insider selling at acme"]
        )

    ok, failed = spans.get_finished_spans()
    assert (ok.name, ok.kind) == ("embeddings embed-test", SpanKind.CLIENT)
    assert dict(ok.attributes or {}) == {
        "gen_ai.operation.name": "embeddings",
        "gen_ai.request.model": "embed-test",
        "filings.embedding.inputs": 1,
    }
    assert failed.status.status_code is StatusCode.ERROR
    for span in (ok, failed):
        assert "acme" not in span.to_json()
