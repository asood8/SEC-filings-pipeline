from datetime import date

import pytest
from pydantic import ValidationError

from filings_common.events import FilingDiscovered, Topic


def make_event(**overrides: object) -> FilingDiscovered:
    fields: dict[str, object] = {
        "accession_no": "0000320193-24-000123",
        "cik": 320193,
        "form_type": "8-K",
        "filed_at": date(2024, 11, 1),
        "raw_object_key": "filings/320193/0000320193-24-000123/doc.htm",
    }
    return FilingDiscovered.model_validate(fields | overrides)


def test_round_trips_through_json() -> None:
    event = make_event()
    assert FilingDiscovered.model_validate_json(event.model_dump_json()) == event
    assert event.schema_version == 1
    assert event.key == "0000320193-24-000123"


@pytest.mark.parametrize("accession_no", ["000032019324000123", "320193-24-000123", "x"])
def test_rejects_malformed_accession_numbers(accession_no: str) -> None:
    with pytest.raises(ValidationError):
        make_event(accession_no=accession_no)


def test_ignores_unknown_fields_from_newer_producers() -> None:
    assert make_event(added_later="value") == make_event()


def test_dead_letter_topic_names() -> None:
    assert Topic.FILINGS_PARSED.dlq == "filings.parsed.dlq"
