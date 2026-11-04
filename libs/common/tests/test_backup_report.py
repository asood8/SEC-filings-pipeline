"""The nightly backup's report step: it records the uploaded dump's size, and nothing at all when
there is no usable dump (so the "backup missing" alert sees the night as missing)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from filings_common import metrics
from filings_common.backup_report import main, report

GAUGE = "filings.backup.size"


@pytest.fixture(autouse=True)
def _no_reported_size() -> Iterator[None]:
    metrics.BACKUP_SIZE_VALUES.remove(report)
    yield
    metrics.BACKUP_SIZE_VALUES.remove(report)


def test_a_dump_is_reported_with_its_size(tmp_path: Path, measured: Any) -> None:
    dump = tmp_path / "filings.dump"
    dump.write_bytes(b"P" * 1234)
    assert report(dump) == 0
    assert measured.gauge(GAUGE) == {"": 1234}


@pytest.mark.parametrize("content", [None, b""], ids=["missing", "empty"])
def test_no_usable_dump_fails_and_reports_nothing(
    tmp_path: Path, measured: Any, content: bytes | None
) -> None:
    dump = tmp_path / "filings.dump"
    if content is not None:
        dump.write_bytes(content)
    assert report(dump) == 1
    assert measured.gauge(GAUGE) == {}


def test_the_command_takes_the_dump_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    dump = tmp_path / "filings.dump"
    dump.write_bytes(b"PGDMP")
    assert main([str(dump)]) == 0
    assert main([str(tmp_path / "nope.dump")]) == 1
