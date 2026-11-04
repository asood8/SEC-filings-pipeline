"""8-K prompt and LLM summarizer, using real 8-K fixtures and fake LLM providers."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import redis

from filings_common.config import GeminiSettings, LlmSettings
from filings_common.consumer import PermanentError
from filings_common.parsed import Exhibit, Form8K
from filings_common.summaries import FilingSummary
from filings_parser.form8k import parse_amendment, parse_form8k
from filings_summarizer.form8k_prompt import (
    AMENDMENT_TASK,
    MAX_ITEM_CHARS,
    MAX_PROMPT_CHARS,
    PROMPT_VERSION,
    SUMMARY_SCHEMA,
    TRUNCATED,
    Amended,
    FilingContext,
    build_prompt,
    truncate,
)
from filings_summarizer.llm import FailoverChain
from filings_summarizer.summarizers import Form8KLlmSummarizer, build_summarizers

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "form8k"
CONTEXT = FilingContext(
    accession_no="0000796343-26-000147",
    form_type="8-K",
    company_name="Adobe Inc.",
    ticker="ADBE",
    filed_at=date(2026, 9, 10),
)


def adobe() -> Form8K:
    return parse_form8k((FIXTURES / "0000796343-26-000147.htm").read_bytes())


def test_prompt_names_the_company_and_every_item() -> None:
    prompt = build_prompt(adobe(), CONTEXT)
    assert prompt.startswith("Company: Adobe Inc. (ADBE)\nForm 8-K filed 2026-09-10")
    assert "## Item 2.02: Results of Operations and Financial Condition" in prompt
    assert "## Item 9.01: Financial Statements and Exhibits" in prompt
    assert "Adobe Reports Record Q3 Results" in prompt


def test_prompt_includes_exhibits() -> None:
    form = adobe().model_copy(
        update={
            "exhibits": [Exhibit(type="EX-99.1", description="Press release", text="Revenue rose.")]
        }
    )
    assert "## Exhibit EX-99.1: Press release\nRevenue rose." in build_prompt(form, CONTEXT)


def test_long_sections_are_cut_at_a_line_boundary() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(1000))
    cut = truncate(text, MAX_ITEM_CHARS)
    assert len(cut) <= MAX_ITEM_CHARS
    assert cut.endswith(f"\n{TRUNCATED}")
    assert cut.removesuffix(f"\n{TRUNCATED}") in text  # nothing invented, nothing reordered


def test_whole_prompt_is_bounded() -> None:
    huge = Exhibit(type="EX-99.1", description=None, text="\n".join(["word " * 20] * 5000))
    form = adobe().model_copy(update={"exhibits": [huge, huge, huge, huge]})
    assert len(build_prompt(form, CONTEXT)) <= MAX_PROMPT_CHARS


def test_schema_matches_the_summary_model() -> None:
    assert set(SUMMARY_SCHEMA["required"]) == set(FilingSummary.model_fields)


class StubProvider:
    name = "gemini"
    model = "gemini-test"

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.answer)


def test_llm_summarizer_records_the_answering_provider() -> None:
    answer = {
        "headline": "Adobe reports record Q3 results",
        "summary": "Adobe published its third-quarter results.",
        "key_points": ["Results press release furnished as Exhibit 99.1"],
    }
    provider = StubProvider(answer)
    summarizer = Form8KLlmSummarizer(
        FailoverChain([provider], validate=FilingSummary.model_validate_json)
    )
    result = summarizer.summarize(adobe().model_dump(mode="json"), CONTEXT)
    assert result.summary == FilingSummary.model_validate(answer)
    assert (result.provider, result.model, result.prompt_version) == (
        "gemini",
        "gemini-test",
        PROMPT_VERSION,
    )
    assert provider.prompts[0].startswith("Company: Adobe Inc. (ADBE)")
    assert result.ungrounded_numbers == []  # "99.1" is in the filing
    assert result.ungrounded_names == []


def test_llm_summarizer_flags_numbers_the_filing_does_not_state() -> None:
    answer = {
        "headline": "Adobe reports $9.9 billion of revenue",  # not in this 8-K's text
        "summary": "Adobe published results on September 10, 2026.",
        "key_points": ["Press release furnished as Exhibit 99.1"],
    }
    summarizer = Form8KLlmSummarizer(FailoverChain([StubProvider(answer)]))
    result = summarizer.summarize(adobe().model_dump(mode="json"), CONTEXT)
    assert result.ungrounded_numbers == ["9.9 billion"]


def test_llm_summarizer_flags_names_the_filing_does_not_use() -> None:
    answer = {
        "headline": "Adobe reports results",
        "summary": "Adobe published results, said its new CFO Jordan Whitfield.",
        "key_points": ["Press release furnished as Exhibit 99.1"],
    }
    summarizer = Form8KLlmSummarizer(FailoverChain([StubProvider(answer)]))
    result = summarizer.summarize(adobe().model_dump(mode="json"), CONTEXT)
    assert result.ungrounded_names == ["Jordan", "Whitfield"]


def test_llm_summarizer_rejects_a_malformed_payload() -> None:
    summarizer = Form8KLlmSummarizer(FailoverChain([StubProvider({})]))
    with pytest.raises(PermanentError, match="cannot read 8-K payload"):
        summarizer.summarize({"items": "nope"}, CONTEXT)


def test_build_summarizers_wires_configured_providers_in_order() -> None:
    client = redis.Redis()  # never contacted while building
    none = build_summarizers(
        LlmSettings(_env_file=None, model=None), GeminiSettings(_env_file=None, api_key=""), client
    )
    assert set(none) == {"4", "4/A"}  # no LLM: 8-Ks and 8-K/As stay parsed

    both = build_summarizers(
        LlmSettings(_env_file=None, model="small"),
        GeminiSettings(_env_file=None, api_key="k"),
        client,
    )
    assert both["8-K/A"] is both["8-K"]  # amendments go through the same chain
    chain = both["8-K"]._chain  # type: ignore[attr-defined]
    assert [p.name for p in chain._providers] == ["ollama", "gemini"]


# --- 8-K/As (ADR-0021) -------------------------------------------------------------------------

NETFLIX_FIX = FIXTURES / "amendments" / "0001193125-25-309911.htm"  # an 8-K/A with a note
AMENDED_CONTEXT = FilingContext(
    accession_no="0001193125-25-309911",
    form_type="8-K/A",
    company_name="Netflix",
    ticker="NFLX",
    filed_at=date(2025, 12, 11),
    amends=Amended(original_filed_at=date(2025, 12, 5)),
)


def netflix_fix() -> Form8K:
    return Form8K.model_validate(parse_amendment(NETFLIX_FIX.read_bytes()))


def test_an_amendments_prompt_says_what_it_amends_and_asks_what_changed() -> None:
    prompt = build_prompt(netflix_fix(), AMENDED_CONTEXT)
    assert prompt.startswith(
        "Company: Netflix (NFLX)\nForm 8-K/A filed 2025-12-11, an amendment to the Form 8-K filed"
        f" 2025-12-05\n{AMENDMENT_TASK}\n\n## Explanatory note\nThis Amendment on Form 8-K/A"
    )
    assert "## Item 9.01" in prompt
    unknown = replace(AMENDED_CONTEXT, amends=Amended(original_filed_at=None))
    assert "an amendment to an earlier 8-K\n" in build_prompt(netflix_fix(), unknown)


def test_an_8ks_prompt_is_unchanged_by_amendments() -> None:
    prompt = build_prompt(adobe(), CONTEXT)
    assert prompt.startswith("Company: Adobe Inc. (ADBE)\nForm 8-K filed 2026-09-10\n\n## Item")
    assert AMENDMENT_TASK not in prompt
    assert "Explanatory note" not in prompt


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Netflix files its merger agreement", "Amended: Netflix files its merger agreement"),
        ("Amended 8-K: Netflix files its merger agreement", None),  # the model said so already
        ("N" * 200, "Amended: " + "N" * 188 + "..."),  # still within the 200 characters
    ],
)
def test_an_amendments_headline_starts_amended(headline: str, expected: str | None) -> None:
    answer = {"headline": headline, "summary": "Netflix filed the agreement.", "key_points": []}
    summarizer = Form8KLlmSummarizer(
        FailoverChain([StubProvider(answer)], validate=FilingSummary.model_validate_json)
    )
    result = summarizer.summarize(netflix_fix().model_dump(mode="json"), AMENDED_CONTEXT)
    assert result.summary.headline == (expected or headline)
    assert len(result.summary.headline) <= 200
    # An 8-K's headline is left as the model wrote it.
    plain = summarizer.summarize(adobe().model_dump(mode="json"), CONTEXT)
    assert plain.summary.headline == headline
