"""The 8-K benchmark harness, with fake models and a real 8-K fixture."""

import json
from datetime import date
from pathlib import Path
from typing import Any

from filings_parser.form8k import parse_form8k
from filings_summarizer.benchmark import (
    Case,
    ModelResult,
    _percentile,
    main,
    render_table,
    rescore,
    run_benchmark,
    write_results,
)
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.llm import LlmError
from filings_summarizer.quality import FilingLabels

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "form8k"
ADOBE = Case(
    parse_form8k((FIXTURES / "0000796343-26-000147.htm").read_bytes()),
    FilingContext(
        accession_no="0000796343-26-000147",
        form_type="8-K",
        company_name="Adobe Inc.",
        ticker="ADBE",
        filed_at=date(2026, 9, 10),
    ),
)


class FakeModel:
    """Answers with a canned summary; `answers` are used in order (an exception is raised)."""

    name = "fake"

    def __init__(self, model: str, *answers: str | Exception) -> None:
        self.model = model
        self._answers = list(answers)
        self.calls = 0

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        self.calls += 1
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def summary(text: str) -> str:
    return json.dumps({"headline": "Adobe reports results", "summary": text, "key_points": []})


def test_benchmark_measures_failures_and_grounding() -> None:
    grounded = FakeModel("good", summary("warmup"), summary("Adobe reported results."))
    sloppy = FakeModel(
        "sloppy",
        summary("warmup"),
        summary("Revenue grew 987654321 percent."),  # a number the filing doesn't contain
    )
    broken = FakeModel("broken", summary("warmup"), "not json")
    offline = FakeModel("offline", LlmError("down"), LlmError("down"))

    results = run_benchmark([grounded, sloppy, broken, offline], [ADOBE])
    stats = {r.model: r.stats() for r in results}

    assert grounded.calls == 2  # one untimed warm-up call, then the measured one
    assert stats["good"] | {"median_s": 0, "p95_s": 0} == {
        "runs": 1,
        "failed": 0,
        "median_s": 0,
        "p95_s": 0,
        "ungrounded_share": 0.0,
        "ungrounded_per_summary": 0.0,
        "summary_words": 3,
    }
    assert stats["sloppy"]["ungrounded_share"] == 1.0
    assert results[1].attempts[0].ungrounded == ["987654321"]
    assert stats["broken"]["failed"] == 1  # off-schema answers count as failures
    assert "ValidationError" in (results[2].attempts[0].error or "")
    assert (stats["offline"]["failed"], stats["offline"]["median_s"]) == (1, None)

    table = render_table(results)
    assert "| fake: good | 1 | 0 |" in table
    assert "| fake: offline | 1 | 1 | n/a | n/a | n/a |" in table


def test_runs_repeat_each_case_without_a_warmup() -> None:
    model = FakeModel("m", *[summary("Adobe reported results.")] * 3)
    [result] = run_benchmark([model], [ADOBE], runs=3, warmup=False)
    assert model.calls == 3
    assert len(result.attempts) == 3


def test_benchmark_scores_summaries_against_labels() -> None:
    labels = {
        ADOBE.context.accession_no: FilingLabels.model_validate(
            {
                "accession_no": ADOBE.context.accession_no,
                "company": "Adobe",
                "facts": [
                    {"fact": "third-quarter results", "patterns": ["third[- ]quarter"]},
                    {"fact": "revenue of $6.76 billion", "numbers": ["6.76 billion"]},
                ],
                "forbidden": [{"claim": "targets lowered", "patterns": ["lowered"]}],
            }
        )
    }
    good = FakeModel("good", summary("warmup"), summary("Third-quarter revenue: $6.76 billion."))
    wrong = FakeModel("wrong", summary("warmup"), summary("Adobe lowered its third-quarter view."))
    broken = FakeModel("broken", summary("warmup"), "not json")

    results = run_benchmark([good, wrong, broken], [ADOBE], labels=labels)

    assert results[0].quality_stats() == {
        "scored": 1,
        "fact_recall": 1.0,
        "forbidden_share": 0.0,
        "forbidden_claims": 0,
    }
    attempt = results[1].attempts[0]
    assert attempt.quality is not None
    assert (attempt.quality.missing, attempt.quality.forbidden) == (
        ["revenue of $6.76 billion"],
        ["targets lowered"],
    )
    assert results[2].quality_stats()["fact_recall"] is None  # failures aren't scored

    good_row, wrong_row, broken_row = render_table(results).splitlines()[2:]
    # Fact recall, then summaries with forbidden claims, after the two timing columns.
    assert good_row.split(" | ")[5:7] == ["100%", "0%"]
    assert wrong_row.split(" | ")[5:7] == ["50%", "100%"]
    assert broken_row.split(" | ")[5:7] == ["n/a", "n/a"]


def test_filings_without_labels_are_not_scored() -> None:
    model = FakeModel("m", summary("warmup"), summary("Adobe reported results."))
    [result] = run_benchmark([model], [ADOBE], labels={})
    assert result.attempts[0].quality is None
    assert result.quality_stats()["scored"] == 0


LOWERED = {
    "accession_no": ADOBE.context.accession_no,
    "company": "Adobe",
    "facts": [{"fact": "third-quarter results", "patterns": ["third[- ]quarter"]}],
    "forbidden": [{"claim": "targets lowered", "patterns": ["lowered"]}],
}


def test_progress_sees_the_results_after_every_attempt(tmp_path: Path) -> None:
    model = FakeModel("m", summary("warmup"), summary("one"), LlmError("down"), summary("three"))
    path = tmp_path / "bench.json"
    saved: list[int] = []

    def save(results: list[ModelResult]) -> None:
        write_results(path, 1, None, results)
        saved.append(len(json.loads(path.read_text())["results"][0]["attempts"]))

    run_benchmark([model], [ADOBE], runs=3, progress=save)

    assert saved == [1, 2, 3]  # an interrupted run keeps every attempt before the interruption
    assert not (tmp_path / "bench.json.partial").exists()


def test_rescore_scores_saved_summaries_against_new_labels(tmp_path: Path) -> None:
    model = FakeModel("m", summary("warmup"), summary("Adobe lowered its targets."), "not json")
    results = run_benchmark([model], [ADOBE], runs=2)  # saved before the labels existed
    path = tmp_path / "bench.json"
    write_results(path, 1, None, results)

    labels = {ADOBE.context.accession_no: FilingLabels.model_validate(LOWERED)}
    [rescored] = rescore(json.loads(path.read_text()), labels)

    assert rescored.stats() == results[0].stats()  # timing, failures, grounding as saved
    assert rescored.attempts[0].quality is not None
    assert rescored.attempts[0].quality.forbidden == ["targets lowered"]
    assert rescored.attempts[1].quality is None  # the failed attempt stays failed
    assert rescored.quality_stats()["forbidden_share"] == 1.0


def test_rescore_from_the_command_line(tmp_path: Path) -> None:
    model = FakeModel("m", summary("warmup"), summary("Adobe's third-quarter results."))
    saved, labels, out = tmp_path / "bench.json", tmp_path / "labels.json", tmp_path / "out.json"
    write_results(saved, 7, None, run_benchmark([model], [ADOBE]))
    labels.write_text(json.dumps({"filings": [LOWERED]}))

    assert main(["--rescore", str(saved), "--labels", str(labels), "--out", str(out)]) == 0

    document = json.loads(out.read_text())
    assert (document["prompt_version"], document["labels"]) == (7, str(labels))
    assert document["results"][0]["stats"]["fact_recall"] == 1.0


def test_percentile_uses_the_nearest_rank() -> None:
    values = [float(n) for n in range(1, 21)]
    assert _percentile(values, 0.95) == 19.0
    assert _percentile(values, 0.5) == 10.0
    assert _percentile([4.0], 0.95) == 4.0
