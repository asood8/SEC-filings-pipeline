"""Request tracing, as when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from filings_api.auth import SESSION_COOKIE

ClientFactory = Callable[..., TestClient]

pytestmark = pytest.mark.integration


def test_each_request_is_a_server_span_named_by_its_route_except_health_checks(
    make_client: ClientFactory, spans: InMemorySpanExporter
) -> None:
    client = make_client(traced=True)
    assert client.get("/healthz").status_code == 200
    assert client.get("/companies/aapl").status_code == 200

    [span] = spans.get_finished_spans()
    assert (span.name, span.kind) == ("GET /companies/{ticker}", SpanKind.SERVER)
    attributes = span.attributes or {}
    assert attributes["http.route"] == "/companies/{ticker}"
    assert attributes["http.status_code"] == 200


def test_sign_in_spans_hold_no_id_token_cookie_or_email(
    make_client: ClientFactory, google: Any, spans: InMemorySpanExporter
) -> None:
    client = make_client(traced=True)
    token = google.token()
    assert client.post("/auth/google", json={"credential": token}).status_code == 200
    session = client.cookies[SESSION_COOKIE]
    assert client.get("/me").status_code == 200

    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == ["POST /auth/google", "GET /me"]
    for span in finished:
        recorded = span.to_json()
        assert token not in recorded
        assert session not in recorded
        assert "ada@example.com" not in recorded
