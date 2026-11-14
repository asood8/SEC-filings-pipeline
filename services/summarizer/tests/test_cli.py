import pytest

from filings_common.config import (
    EmbeddingSettings,
    GeminiSettings,
    LlmSettings,
    SummarizerSettings,
)
from filings_summarizer import cli


def test_the_poll_interval_covers_every_retry_of_one_message() -> None:
    local_only = cli.poll_interval_seconds(
        LlmSettings(_env_file=None, model="llama3.2:3b", timeout_seconds=600),
        GeminiSettings(_env_file=None, api_key=""),
        EmbeddingSettings(_env_file=None),
    )
    # 6 attempts x 600 s, plus 10 + 20 + 40 + 80 + 160 s of backoff, plus the margin.
    assert local_only == 6 * 600 + 310 + 60
    everything = cli.poll_interval_seconds(
        LlmSettings(_env_file=None, model="m", timeout_seconds=600),
        GeminiSettings(_env_file=None, api_key="k", timeout_seconds=60),
        EmbeddingSettings(_env_file=None, model="e", timeout_seconds=60),
    )
    assert everything == 6 * 720 + 310 + 60


def test_one_attempt_is_allowed_every_model_timeout_plus_a_margin() -> None:
    # The liveness heartbeat's allowance per attempt: never shorter than a working summary.
    everything = cli.attempt_seconds(
        LlmSettings(_env_file=None, model="m", timeout_seconds=600),
        GeminiSettings(_env_file=None, api_key="k", timeout_seconds=60),
        EmbeddingSettings(_env_file=None, model="e", timeout_seconds=60),
    )
    assert everything == 600 + 60 + 60 + cli.ATTEMPT_MARGIN_SECONDS
    gemini_only = cli.attempt_seconds(
        LlmSettings(_env_file=None),
        GeminiSettings(_env_file=None, api_key="k", timeout_seconds=60),
        EmbeddingSettings(_env_file=None),
    )
    assert gemini_only == 60 + cli.ATTEMPT_MARGIN_SECONDS


def test_the_poll_interval_stays_within_librdkafkas_limits() -> None:
    nothing = cli.poll_interval_seconds(
        LlmSettings(_env_file=None),
        GeminiSettings(_env_file=None, api_key=""),
        EmbeddingSettings(_env_file=None),
    )
    assert nothing == cli.MIN_POLL_INTERVAL_SECONDS
    huge = cli.poll_interval_seconds(
        LlmSettings(_env_file=None, model="m", timeout_seconds=100_000),
        GeminiSettings(_env_file=None, api_key=""),
        EmbeddingSettings(_env_file=None),
    )
    assert huge == cli.MAX_POLL_INTERVAL_SECONDS


def test_one_queue_takes_every_form_unless_some_get_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUMMARIZER_SEPARATE_FORMS", raising=False)
    [only] = cli.queues(SummarizerSettings(_env_file=None))
    assert (only.process_name, only.consumer_group, only.forms, only.skip) == (
        "filings-summarizer",
        "summarizer",
        frozenset(),
        frozenset(),
    )
    assert only.uses_llm

    # Production: Form 4s in a queue of their own, so they never wait behind an 8-K's LLM call.
    monkeypatch.setenv("SUMMARIZER_SEPARATE_FORMS", " 4 ,")
    main, form4 = cli.queues(SummarizerSettings(_env_file=None))
    assert (main.consumer_group, main.skip, main.uses_llm) == ("summarizer", frozenset({"4"}), True)
    assert (form4.process_name, form4.consumer_group, form4.forms) == (
        "filings-summarizer-form4",
        "summarizer-form4",
        frozenset({"4"}),
    )
    assert not form4.uses_llm  # templates only: its poll interval allows no LLM timeout
