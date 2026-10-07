"""Request tracing and metrics, as when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

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
    assert attributes["http.response.status_code"] == 200


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


def test_requests_are_measured_by_route_and_status_but_never_by_host_or_size(
    make_client: ClientFactory, measured: Any
) -> None:
    client = make_client(traced=True)
    assert client.get("/healthz").status_code == 200
    assert client.get("/companies/aapl").status_code == 200
    assert client.get("/companies/nope").status_code == 404
    client.get("/companies/aapl", headers={"Host": "random-123.example"})

    series = measured.attribute_sets("http.server.request.duration")
    assert {(s.get("http.route"), s["http.response.status_code"]) for s in series} == {
        ("/companies/{ticker}", 200),
        ("/companies/{ticker}", 404),
    }  # health checks aren't measured
    assert measured.count("http.server.request.duration") == 3
    for attributes in series:  # bounded values only: no host, path, client, or user agent
        assert set(attributes) <= {
            "http.request.method",
            "http.route",
            "http.response.status_code",
            "url.scheme",
            "network.protocol.version",
            "error.type",
        }
    assert not {"http.server.request.body.size", "http.server.response.body.size"} & (
        measured.names()
    )
