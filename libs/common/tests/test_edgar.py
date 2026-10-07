import random
from typing import Any

import httpx2
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from filings_common.config import EdgarSettings
from filings_common.edgar import EdgarClient, archive_url


class CountingLimiter:
    def __init__(self, wait: float = 0.0) -> None:
        self.calls = 0
        self.wait = wait

    def acquire(self) -> float:
        self.calls += 1
        return self.wait


class Harness:
    """An EdgarClient wired to canned responses, recording requests, tokens, and sleeps."""

    def __init__(self, *responses: httpx2.Response | Exception, max_attempts: int = 5) -> None:
        self.requests: list[httpx2.Request] = []
        self.sleeps: list[float] = []
        self.limiter = CountingLimiter()
        pending = list(responses)

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            response = pending.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        settings = EdgarSettings(
            _env_file=None, contact_email="ops@example.com", max_attempts=max_attempts
        )
        self.client = EdgarClient(
            settings,
            self.limiter,
            transport=httpx2.MockTransport(handler),
            sleep=self.sleeps.append,
            rng=random.Random(0),
        )


def test_every_request_declares_user_agent_and_takes_a_token() -> None:
    h = Harness(httpx2.Response(200, json={"ok": 1}), httpx2.Response(200, json={"ok": 2}))
    h.client.get_json("https://www.sec.gov/a")
    h.client.get_json("https://www.sec.gov/b")
    user_agents = {r.headers["User-Agent"] for r in h.requests}
    assert user_agents == {"sec-filings-pipeline ops@example.com"}
    assert h.limiter.calls == 2


def test_retries_429_and_honors_retry_after() -> None:
    h = Harness(httpx2.Response(429, headers={"Retry-After": "7"}), httpx2.Response(200, text="ok"))
    assert h.client.get("https://www.sec.gov/x").text == "ok"
    assert h.sleeps == [7.0]
    assert h.limiter.calls == 2  # the retry takes a token too


def test_retries_network_errors() -> None:
    h = Harness(httpx2.ConnectError("connection refused"), httpx2.Response(200, text="ok"))
    assert h.client.get("https://www.sec.gov/x").text == "ok"
    assert len(h.sleeps) == 1


def test_gives_up_after_max_attempts() -> None:
    h = Harness(*[httpx2.Response(503)] * 3, max_attempts=3)
    with pytest.raises(httpx2.HTTPStatusError):
        h.client.get("https://www.sec.gov/x")
    assert len(h.requests) == 3
    assert len(h.sleeps) == 2


def test_backoff_grows_exponentially_with_jitter_and_is_capped() -> None:
    h = Harness(*[httpx2.Response(503)] * 9, httpx2.Response(200), max_attempts=10)
    h.client.get("https://www.sec.gov/x")
    for attempt, delay in enumerate(h.sleeps, start=1):
        ceiling = min(60.0, 2.0 ** (attempt - 1))
        assert ceiling / 2 <= delay <= ceiling


def test_client_errors_are_not_retried() -> None:
    h = Harness(httpx2.Response(404))
    with pytest.raises(httpx2.HTTPStatusError):
        h.client.get("https://www.sec.gov/missing")
    assert h.sleeps == []
    assert h.limiter.calls == 1


def test_submissions_url_uses_ten_digit_cik() -> None:
    h = Harness(httpx2.Response(200, json={"cik": "320193"}))
    h.client.submissions(320193)
    assert str(h.requests[0].url) == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_archive_url_strips_dashes_from_accession_number() -> None:
    assert (
        archive_url(320193, "0000320193-24-000123", "doc.htm")
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/doc.htm"
    )


def test_each_request_is_one_client_span_with_its_retries_and_rate_limit_wait(
    spans: InMemorySpanExporter,
) -> None:
    h = Harness(httpx2.Response(503), httpx2.Response(200, text="ok"))
    h.limiter.wait = 0.25
    h.client.get("https://www.sec.gov/Archives/edgar/data/320193/x.htm")

    [span] = spans.get_finished_spans()
    assert (span.name, span.kind) == ("GET", SpanKind.CLIENT)
    assert dict(span.attributes or {}) == {
        "http.request.method": "GET",
        "url.full": "https://www.sec.gov/Archives/edgar/data/320193/x.htm",
        "server.address": "www.sec.gov",
        "filings.rate_limit.wait_seconds": 0.5,  # both attempts waited for a token
        "http.request.resend_count": 1,
        "http.response.status_code": 200,
    }
    assert [(e.name, dict(e.attributes or {})) for e in span.events] == [
        ("retry", {"http.response.status_code": 503})
    ]


def test_a_failed_request_marks_its_span_failed(spans: InMemorySpanExporter) -> None:
    h = Harness(httpx2.Response(404))
    with pytest.raises(httpx2.HTTPStatusError):
        h.client.get("https://www.sec.gov/missing")
    [span] = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})["http.response.status_code"] == 404


def test_each_attempt_is_measured_by_status_class_with_429s_counted_on_their_own(
    measured: Any,
) -> None:
    h = Harness(
        httpx2.Response(429),
        httpx2.ConnectError("reset"),
        httpx2.Response(200, text="ok"),
        httpx2.Response(404),
    )
    h.limiter.wait = 0.2
    h.client.get("https://www.sec.gov/a")
    with pytest.raises(httpx2.HTTPStatusError):
        h.client.get("https://www.sec.gov/missing")

    def responses(status_class: str) -> float:
        return float(
            measured.total("filings.sec.responses", {"filings.status_class": status_class})
        )

    assert [responses(c) for c in ("2xx", "4xx", "error")] == [1, 2, 1]
    assert measured.total("filings.sec.throttled") == 1
    assert measured.count("filings.sec.rate_limit.wait") == 4  # every attempt waits its turn
    assert measured.attribute_sets("filings.sec.rate_limit.wait") == [{}]
