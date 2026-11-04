"""Earnings-call Q&A summaries (ADR-0022), with fake LLM providers. The transcripts are written
for these tests in the shape of real ones (the real ones can't be redistributed)."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from pymongo.database import Database
from sqlalchemy.engine import Engine

from filings_common.db import CallSummary, CallTranscript, Company, session_factory
from filings_common.mongo import TranscriptStore
from filings_common.summaries import FilingSummary
from filings_summarizer.call_benchmark import load_cases, run
from filings_summarizer.calls import (
    CALL_PROMPT_VERSION,
    EXCHANGE_SYSTEM,
    TAKEAWAYS_SYSTEM,
    CallContext,
    ExchangeSummary,
    summarize_call,
    takeaways_from_json,
    takeaways_prompt,
)
from filings_summarizer.calls_job import pending, summarize_calls
from filings_summarizer.llm import FailoverChain, GeminiProvider, LlmError, OpenAICompatibleProvider
from filings_summarizer.quality import ExchangeLabels, LabelError, load_call_labels

CONTEXT = CallContext("Example Corp", "EXM", date(2026, 10, 13))
TRANSCRIPT = """\
Example Corp third quarter 2026 earnings call. Jane Roe, Chief Financial Officer: revenue grew.
Operator: Our first question comes from John Smith with Big Bank. Your line is open.
John Smith: How did deposits do, and what about costs?
Jane Roe: Deposits grew 3% to $40 billion. Costs were flat.
Operator: Our next question comes from Mary Major with Other Bank.
Mary Major: Any update on buybacks?
Jane Roe: We bought back $2 billion of stock this quarter.
"""


class Stub:
    """Answers like the model would: one exchange's summary, or the takeaways."""

    name = "ollama"
    model = "stub"

    def __init__(self, exchange_answer: str = "Deposits grew 3% to $40 billion, said Jane Roe."):
        self.exchange_answer = exchange_answer
        self.prompts: list[tuple[str, str]] = []

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        self.prompts.append((system, prompt))
        if system == EXCHANGE_SYSTEM:
            analyst = prompt.split("Analyst: ", 1)[1].split(",", 1)[0]
            return json.dumps(
                {"question": f"{analyst} asked a question.", "answer": self.exchange_answer}
            )
        return json.dumps(
            {
                "headline": "Example Corp: deposits and buybacks",
                "summary": "Analysts asked about deposits and buybacks.",
                "key_points": ["Deposits grew 3%", "Buybacks of $2 billion"],
            }
        )


def chains(provider: Any) -> tuple[FailoverChain, FailoverChain]:
    return (
        FailoverChain([provider], validate=ExchangeSummary.model_validate_json),
        FailoverChain([provider], validate=FilingSummary.model_validate_json),
    )


def test_each_exchange_is_summarized_then_the_takeaways() -> None:
    stub = Stub()
    result = summarize_call(*chains(stub), CONTEXT, TRANSCRIPT)
    assert result is not None
    assert [(x["analyst"], x["firm"], x["question"]) for x in result.exchanges] == [
        ("John Smith", "Big Bank", "John Smith asked a question."),
        ("Mary Major", "Other Bank", "Mary Major asked a question."),
    ]
    assert [system for system, _ in stub.prompts] == [EXCHANGE_SYSTEM] * 2 + [TAKEAWAYS_SYSTEM]
    first = stub.prompts[0][1]
    assert first.startswith(
        "Company: Example Corp (EXM)\nEarnings call on 2026-10-13\n"
        "Analyst: John Smith, Big Bank\n\nOperator: Our first question"
    )
    assert "buybacks" not in first  # an exchange's prompt holds only that exchange
    assert result.takeaways.headline == "Example Corp: deposits and buybacks"
    assert (result.provider, result.model) == ("ollama", "stub")
    assert result.exchanges[0]["ungrounded_numbers"] == []  # 3% and $40 billion are in it
    # The same answer for the second exchange states figures its own text doesn't have.
    assert result.exchanges[1]["ungrounded_numbers"] == ["3", "40 billion"]
    # "3%" is in the transcript, but the takeaways repeat it from the second exchange's summary,
    # where it was flagged.
    assert result.ungrounded_numbers == ["3"]


def test_a_transcript_without_questions_gets_no_summary() -> None:
    stub = Stub()
    assert summarize_call(*chains(stub), CONTEXT, "Prepared remarks only.") is None
    assert stub.prompts == []


def test_the_takeaways_prompt_lists_every_exchange_summary() -> None:
    prompt = takeaways_prompt(
        CONTEXT,
        [
            {"analyst": "John Smith", "firm": "Big Bank", "question": "Deposits?", "answer": "Up."},
        ],
    )
    assert prompt.endswith("\n- John Smith (Big Bank) asked: Deposits? Answer: Up.")


def captured(
    transport_bodies: list[dict[str, Any]], answer: dict[str, Any]
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        transport_bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=answer)

    return httpx2.MockTransport(handler)


def test_providers_cap_the_answer_only_when_asked() -> None:
    bodies: list[dict[str, Any]] = []
    openai_answer = {"choices": [{"message": {"content": "{}"}}]}
    for cap in (None, 400):
        OpenAICompatibleProvider(
            name="ollama",
            base_url="http://llm",
            model="m",
            timeout=1,
            max_output_tokens=cap,
            transport=captured(bodies, openai_answer),
        ).generate_json("s", "p", {})
    assert "max_tokens" not in bodies[0]  # 8-K summaries are unchanged
    assert bodies[1]["max_tokens"] == 400
    gemini_answer = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
    GeminiProvider(
        api_key="k",
        model="g",
        timeout=1,
        max_output_tokens=400,
        transport=captured(bodies, gemini_answer),
    ).generate_json("s", "p", {})
    assert bodies[2]["generationConfig"]["maxOutputTokens"] == 400


# --- The nightly job ------------------------------------------------------------------------------

CIK = 1001
SOURCE = "https://example.org/q3-2026.pdf"


def seed(engine: Engine, mongo_db: Database[dict[str, Any]], *days: date) -> None:
    with session_factory(engine).begin() as session:
        session.add(Company(cik=CIK, ticker="EXM", name="EXAMPLE CORP", on_watchlist=True))
        session.flush()
        for day in days:
            url = f"{SOURCE}?{day}"
            session.add(
                CallTranscript(
                    cik=CIK, call_date=day, source_url=url, text_sha256="x", words=60, exchanges=2
                )
            )
            TranscriptStore(mongo_db).upsert(
                source_url=url, cik=CIK, ticker="EXM", call_date=day, text=TRANSCRIPT
            )


def rows(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                sa.select(
                    CallSummary.transcript_id,
                    CallSummary.provider,
                    CallSummary.prompt_version,
                    CallSummary.takeaways["headline"].astext,
                ).order_by(CallSummary.transcript_id)
            )
        ]


@pytest.mark.integration
def test_the_job_summarizes_each_call_once_newest_first(
    clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    seed(clean_db, mongo_db, date(2026, 7, 14), date(2026, 10, 13))
    sessions = session_factory(clean_db)
    with sessions() as session:
        assert [p.context.call_date for p in pending(session)] == [
            date(2026, 10, 13),
            date(2026, 7, 14),
        ]
        assert pending(session)[0].context.company_name == "Example Corp"
    report = summarize_calls(sessions, TranscriptStore(mongo_db), *chains(Stub()))
    assert report.summarized == ["EXM 2026-10-13", "EXM 2026-07-14"]
    assert rows(clean_db) == [
        (1, "ollama", CALL_PROMPT_VERSION, "Example Corp: deposits and buybacks"),
        (2, "ollama", CALL_PROMPT_VERSION, "Example Corp: deposits and buybacks"),
    ]
    with sessions() as session:
        assert pending(session) == []
    again = summarize_calls(sessions, TranscriptStore(mongo_db), *chains(Stub()))
    assert again.summarized == []
    assert len(rows(clean_db)) == 2


class Down:
    name = "ollama"
    model = "stub"

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        raise LlmError("ollama unreachable")


@pytest.mark.integration
def test_failed_calls_and_calls_past_the_budget_wait_for_the_next_run(
    clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    seed(clean_db, mongo_db, date(2026, 7, 14), date(2026, 10, 13))
    sessions = session_factory(clean_db)
    failed = summarize_calls(sessions, TranscriptStore(mongo_db), *chains(Down()))
    assert failed.failed == ["EXM 2026-10-13", "EXM 2026-07-14"]
    assert rows(clean_db) == []

    ticks = iter([0.0, 0.0, 10.0])  # the budget runs out after the first call
    late = summarize_calls(
        sessions,
        TranscriptStore(mongo_db),
        *chains(Stub()),
        budget_seconds=5,
        clock=lambda: next(ticks),
    )
    assert (late.summarized, late.left) == (["EXM 2026-10-13"], 1)


# --- The benchmark -------------------------------------------------------------------------------


def test_the_call_label_file_is_valid() -> None:
    labels = load_call_labels(Path(__file__).parents[3] / "config" / "call_summary_eval.json")
    assert {(x.ticker, x.analyst) for x in labels} >= {("JPM", "Matt O'Connor")}
    assert all(len(x.facts) >= 3 for x in labels)


def test_exchanges_labeled_twice_are_refused(tmp_path: Any) -> None:
    exchange = {
        "ticker": "EXM",
        "call_date": "2026-10-13",
        "analyst": "John Smith",
        "facts": [{"fact": "f", "patterns": ["x"]}],
    }
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"exchanges": [exchange, exchange]}))
    with pytest.raises(LabelError, match="labeled twice"):
        load_call_labels(path)


LABELS = [
    ExchangeLabels.model_validate(
        {
            "ticker": "EXM",
            "call_date": "2026-10-13",
            "analyst": "John Smith",
            "facts": [
                {"fact": "deposits grew", "patterns": ["deposits grew"]},
                {"fact": "$40 billion", "numbers": ["40 billion"]},
                {"fact": "costs flat", "patterns": ["flat"]},
            ],
            "forbidden": [{"claim": "deposits fell", "patterns": ["deposits fell"]}],
        }
    ),
    ExchangeLabels.model_validate(
        {
            "ticker": "EXM",
            "call_date": "2026-01-13",
            "analyst": "Nobody",
            "facts": [{"fact": "f", "patterns": ["x"]}],
        }
    ),
]


@pytest.mark.integration
def test_the_benchmark_scores_the_labeled_exchanges_it_finds(
    clean_db: Engine, mongo_db: Database[dict[str, Any]]
) -> None:
    seed(clean_db, mongo_db, date(2026, 10, 13))
    cases, missing = load_cases(session_factory(clean_db), TranscriptStore(mongo_db), LABELS)
    assert [c.exchange.analyst for c in cases] == ["John Smith"]
    assert missing == ["EXM 2026-01-13 Nobody"]
    [result] = run([Stub()], cases, runs=2)
    assert result.row() == {
        "model": "ollama:stub",
        "summaries": 2,
        "failed": 0,
        "median_s": 0.0,
        "max_s": 0.0,
        "fact_recall": 0.67,
        "forbidden_share": 0.0,
        "ungrounded": 0,
    }
    assert result.summaries[0]["missing"] == ["costs flat"]


class FailsFor:
    """The stub, except that it fails for the given analysts' exchanges."""

    name = "ollama"
    model = "stub"

    def __init__(self, *analysts: str) -> None:
        self.analysts = analysts
        self.stub = Stub()

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        if system == EXCHANGE_SYSTEM and any(f"Analyst: {a}," in prompt for a in self.analysts):
            raise LlmError("answer cut off")
        return self.stub.generate_json(system, prompt, schema)


def test_one_failed_exchange_is_listed_and_the_call_still_summarized() -> None:
    provider = FailsFor("Mary Major")
    result = summarize_call(*chains(provider), CONTEXT, TRANSCRIPT)
    assert result is not None
    assert result.exchanges[1] == {"analyst": "Mary Major", "firm": "Other Bank", "failed": True}
    takeaways_prompt_sent = provider.stub.prompts[-1][1]
    assert "Mary Major" not in takeaways_prompt_sent


def test_a_call_where_most_exchanges_fail_is_left_for_the_next_run() -> None:
    with pytest.raises(LlmError, match="2 of 2 exchanges failed"):
        summarize_call(*chains(FailsFor("John Smith", "Mary Major")), CONTEXT, TRANSCRIPT)


def test_a_long_question_or_answer_is_clipped_not_refused() -> None:
    long = "Can you talk about deposits, " * 40
    summary = ExchangeSummary.model_validate({"question": long, "answer": "Up 3%."})
    assert len(summary.question) <= 600
    assert summary.question.endswith("...")
    assert long.startswith(summary.question.removesuffix("..."))  # cut at a word
    assert summary.answer == "Up 3%."


class MadeUp(Stub):
    """An exchange summary that makes up a figure, and takeaways that repeat it: the number is
    elsewhere in the transcript ("3%"), so only the exchange check sees it's not in that one."""

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        if system == EXCHANGE_SYSTEM and "Analyst: Mary Major," in prompt:
            return json.dumps({"question": "Buybacks?", "answer": "Up 3%, said Jane Roe."})
        if system == TAKEAWAYS_SYSTEM:
            return json.dumps(
                {
                    "headline": "Example Corp: buybacks",
                    "summary": "Buybacks rose 3%.",
                    "key_points": ["Jane Roe on buybacks"],
                }
            )
        return super().generate_json(system, prompt, schema)


def test_takeaways_repeating_a_figure_an_exchange_made_up_are_flagged() -> None:
    result = summarize_call(*chains(MadeUp()), CONTEXT, TRANSCRIPT)
    assert result is not None
    assert result.exchanges[1]["ungrounded_numbers"] == ["3"]
    assert result.ungrounded_numbers == ["3"]  # "3%" is in the transcript, not in that exchange


def test_takeaways_are_fitted_to_the_summary_shape_not_refused() -> None:
    long = "JPMorgan Chase: analysts ask about deposits, credit, capital and the consumer " * 4
    takeaways = takeaways_from_json(
        json.dumps(
            {
                "headline": long,
                "summary": "Analysts asked about deposits.",
                "key_points": ["", *[f"Point {n}" for n in range(1, 11)], "  "],
            }
        )
    )
    assert len(takeaways.headline) <= 200
    assert takeaways.headline.endswith("...")
    assert long.startswith(takeaways.headline.removesuffix("..."))
    assert takeaways.key_points == [f"Point {n}" for n in range(1, 9)]
    with pytest.raises(ValueError, match="Unterminated"):
        takeaways_from_json('{"headline": "Cut off at the cap')  # still a failure


def test_the_company_name_from_the_prompt_is_not_an_unknown_name() -> None:
    # The transcript writes the name as one word; the model takes it from the prompt's header.
    context = CallContext("JPMorgan Chase & Co.", "JPM", date(2026, 10, 13))
    transcript = TRANSCRIPT.replace("Example Corp", "JPMorganChase")

    class Named(Stub):
        def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
            if system == TAKEAWAYS_SYSTEM:
                return json.dumps(
                    {"headline": "JPMorgan Chase: deposits", "summary": "Fine.", "key_points": []}
                )
            return super().generate_json(system, prompt, schema)

    result = summarize_call(*chains(Named()), context, transcript)
    assert result is not None
    assert result.ungrounded_names == []
