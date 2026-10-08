"""Summary quality scoring and the label loader. The last tests run the real label file against
summaries the pipeline actually wrote, including the known slips."""

import json
from pathlib import Path
from typing import Any

import pytest

from filings_common.summaries import FilingSummary
from filings_summarizer.quality import (
    FilingLabels,
    LabelError,
    QualityScore,
    load_labels,
    plain,
    quality_stats,
    score_summary,
)

LABELS = Path(__file__).parents[3] / "config" / "summary_eval.json"
NIKE_VOTES = "0000320187-26-000143"
ORACLE_PLAN = "0001193125-26-389753"
TESLA_CREDIT = "0001628280-26-063820"


def summary(headline: str, text: str = "Nothing more.", *points: str) -> FilingSummary:
    return FilingSummary(headline=headline, summary=text, key_points=list(points))


def labels(**fields: Any) -> FilingLabels:
    return FilingLabels.model_validate({"accession_no": "0000000000-26-000001"} | fields)


ACME = labels(
    company="Acme",
    facts=[
        {"fact": "CEO named", "patterns": ["smith.{0,40}(chief executive|ceo)"]},
        {"fact": "16,000,000 shares", "numbers": ["16,000,000"]},
        {"fact": "$1.0 billion loan", "patterns": ["loan"], "numbers": ["1.0 billion"]},
    ],
    forbidden=[{"claim": "all passed", "patterns": [r"\ball proposals passed\b"]}],
)


def test_facts_match_patterns_and_numbers_by_value() -> None:
    score = score_summary(
        summary(
            "Acme names Smith CEO",
            "Acme added 16 million shares and took a $1 billion loan.",
        ),
        ACME,
    )
    assert score == QualityScore(facts=3, stated=3, fact_recall=1.0, missing=[], forbidden=[])


def test_a_fact_with_patterns_and_numbers_needs_both() -> None:
    score = score_summary(summary("Acme names Smith CEO", "It took a $2 billion loan."), ACME)
    assert score.missing == ["16,000,000 shares", "$1.0 billion loan"]
    assert score.fact_recall == pytest.approx(1 / 3, abs=1e-4)


def test_patterns_never_span_two_parts_of_a_summary() -> None:
    # "Smith" ends the headline and "CEO" starts the summary: not one statement.
    score = score_summary(summary("Acme thanks Smith", "CEO pay was 16,000,000."), ACME)
    assert "CEO named" in score.missing


def test_curly_quotes_and_dashes_are_made_plain() -> None:
    curly = (
        f"McDonald{chr(0x2019)}s {chr(0x201C)}NEXT{chr(0x201D)}{chr(0xA0)}plan {chr(0x2013)} 2030"
    )
    assert plain(curly) == ('McDonald\'s "NEXT" plan - 2030')


def test_forbidden_claims_are_listed() -> None:
    score = score_summary(summary("Acme meeting", "All proposals passed."), ACME)
    assert score.forbidden == ["all passed"]


def test_quality_stats_average_per_summary() -> None:
    perfect = QualityScore(facts=2, stated=2, fact_recall=1.0, missing=[], forbidden=[])
    half = QualityScore(facts=4, stated=2, fact_recall=0.5, missing=["a", "b"], forbidden=["x"])
    assert quality_stats([perfect, half]) == {
        "scored": 2,
        "fact_recall": 0.75,
        "forbidden_share": 0.5,
        "forbidden_claims": 1,
    }
    assert quality_stats([]) == {
        "scored": 0,
        "fact_recall": None,
        "forbidden_share": None,
        "forbidden_claims": None,
    }


def write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(document))
    return path


def filing(**overrides: Any) -> dict[str, Any]:
    return {
        "accession_no": "0000000000-26-000001",
        "company": "Acme",
        "facts": [{"fact": "f", "patterns": ["x"]}],
    } | overrides


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"filings": []}, "at least 1"),
        ({"filings": [filing(accession_no="123")]}, "pattern"),
        ({"filings": [filing(facts=[])]}, "at least 1"),
        ({"filings": [filing(facts=[{"fact": "f"}])]}, "needs patterns or numbers"),
        ({"filings": [filing(facts=[{"fact": "f", "patterns": ["(unclosed"]}])]}, "regular"),
        ({"filings": [filing(facts=[{"fact": "f", "numbers": ["1 and 2"]}])]}, "exactly one"),
        ({"filings": [filing(facts=[{"fact": "f", "numbers": ["none"]}])]}, "exactly one"),
        ({"filings": [filing(forbidden=[{"claim": "c", "patterns": []}])]}, "at least 1"),
        ({"filings": [filing(), filing()]}, "labeled twice"),
        ({"filings": [filing(extra=1)]}, "Extra inputs"),
    ],
)
def test_malformed_labels_are_rejected(tmp_path: Path, document: object, message: str) -> None:
    with pytest.raises(LabelError, match=message):
        load_labels(write(tmp_path, document))


def test_unreadable_label_files_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(LabelError, match="can't read"):
        load_labels(tmp_path / "missing.json")
    (tmp_path / "broken.json").write_text("{")
    with pytest.raises(LabelError, match="not a valid label file"):
        load_labels(tmp_path / "broken.json")


# The real label file.


def test_the_label_file_covers_every_collected_8k() -> None:
    labeled = load_labels(LABELS)
    assert len(labeled) == 22
    assert all(len(f.facts) >= 3 for f in labeled.values())


def test_known_slips_are_caught() -> None:
    labeled = load_labels(LABELS)
    # ADR-0010: llama3.2:3b's headline named the wrong actor.
    oracle = score_summary(
        summary(
            "Oracle Corp Cancels Larry Ellison's Plan to Sell Oracle Stock",
            "Larry Ellison has cancelled his 10b5-1 Plan to sell Oracle stock.",
        ),
        labeled[ORACLE_PLAN],
    )
    assert oracle.forbidden == ["Oracle cancelled Ellison's plan"]
    # ADR-0010: both small models said every proposal passed; two shareholder proposals failed.
    nike = score_summary(
        summary("NIKE shareholders approve all proposals at annual meeting"),
        labeled[NIKE_VOTES],
    )
    assert nike.forbidden == ["all proposals passed"]
    tesla = score_summary(
        summary(
            "Tesla secures credit facilities",
            "Tesla entered into new credit agreements.",
            "Term Loan Facility: $20.0 billion, 18-month delayed draw term loan facility",
            "Total commitments: up to $14.0 billion across the Revolving Facilities",
        ),
        labeled[TESLA_CREDIT],
    )
    assert len(tesla.forbidden) == 2
    assert "the existing revolver was terminated" in tesla.missing


def test_correct_summaries_score_clean() -> None:
    labeled = load_labels(LABELS)
    # Gemini's stored summary of the Oracle 8-K.
    oracle = score_summary(
        summary(
            "Oracle Corp: Larry Ellison Cancels 10b5-1 Stock Sale Plan",
            "Oracle Corporation announced that Larry Ellison, Executive Chair of the Board and"
            " Chief Technology Officer, has cancelled his 10b5-1 Plan to sell Oracle stock. The"
            " press release issued on September 12, 2026, confirmed that no shares were sold under"
            " the plan prior to its termination. Furthermore, Ellison currently has no other plans"
            " to sell any of his Oracle stock.",
        ),
        labeled[ORACLE_PLAN],
    )
    assert (oracle.fact_recall, oracle.forbidden) == (1.0, [])
    nike = score_summary(
        summary(
            "NIKE shareholders approve stock purchase plan; shareholder proposals fail",
            "At the September 8, 2026 annual meeting, shareholders approved the amended Employee"
            " Stock Purchase Plan, adding 16 million Class B shares, ratified PwC as auditor, and"
            " rejected two shareholder proposals.",
        ),
        labeled[NIKE_VOTES],
    )
    assert (nike.fact_recall, nike.forbidden) == (1.0, [])
