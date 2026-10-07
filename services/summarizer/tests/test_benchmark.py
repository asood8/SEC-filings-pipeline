"""The 8-K benchmark harness, with fake models and a real 8-K fixture."""

import json
from datetime import date
from pathlib import Path
from typing import Any

from filings_parser.form8k import parse_form8k
from filings_summarizer.benchmark import Case, _percentile, render_table, run_benchmark
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.llm import LlmError

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


def test_percentile_uses_the_nearest_rank() -> None:
    values = [float(n) for n in range(1, 21)]
    assert _percentile(values, 0.95) == 19.0
    assert _percentile(values, 0.5) == 10.0
    assert _percentile([4.0], 0.95) == 4.0
