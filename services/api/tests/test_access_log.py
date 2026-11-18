"""uvicorn's access log never holds an unsubscribe token (ADR-0012), as traces don't."""

import logging

import pytest

from filings_api.app import AccessLogTokenFilter, redact_access_log

ACCESS_LINE = '%s - "%s %s HTTP/%s" %d'  # uvicorn's
SIGNED = "99.c2lnbmVkLXRva2Vu"


def access_line(method: str, path: str) -> str:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        ACCESS_LINE,
        ("10.42.0.7:51234", method, path, "1.1", 200),
        None,
    )
    assert AccessLogTokenFilter().filter(record)  # never drops the line
    return record.getMessage()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unsubscribe_tokens_are_redacted(method: str) -> None:
    line = access_line(method, f"/unsubscribe?token={SIGNED}")
    assert SIGNED not in line
    assert line == f'10.42.0.7:51234 - "{method} /unsubscribe?token=REDACTED HTTP/1.1" 200'


@pytest.mark.parametrize(
    "path",
    ["/search?q=buyback&mode=hybrid", "/unsubscribe", "/filings?token=not-a-credential"],
)
def test_other_lines_are_unchanged(path: str) -> None:
    assert access_line("GET", path) == f'10.42.0.7:51234 - "GET {path} HTTP/1.1" 200'


def test_the_filter_is_added_once(monkeypatch: pytest.MonkeyPatch) -> None:
    access_log = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access_log, "filters", [])
    redact_access_log()
    redact_access_log()  # a second app in the same process
    assert [type(f) for f in access_log.filters] == [AccessLogTokenFilter]


def test_a_logged_line_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    access_log = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access_log, "filters", [])
    redact_access_log()
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access_log.info(
            ACCESS_LINE, "10.42.0.7:51234", "POST", f"/unsubscribe?token={SIGNED}", "1.1", 200
        )
    assert SIGNED not in caplog.text
    assert "/unsubscribe?token=REDACTED" in caplog.text
