"""Alerts about earnings calls' Q&A summaries (ADR-0022): who is alerted, and only once. How
each channel shows them is in test_channels."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.alert_filters import CALL_QA
from filings_common.consumer import PermanentError, Received
from filings_common.db import (
    CallSummary,
    CallTranscript,
    Channel,
    Company,
    Notification,
    NotificationStatus,
    Subscription,
    User,
    session_factory,
)
from filings_common.events import CallSummarized, Topic
from filings_notifier.channels import Alert
from filings_notifier.handler import CallNotificationHandler, TopicHandler

SOURCE = "https://www.microsoft.com/en-us/investor/events/fy-2027/earnings-fy-2027-q1"
SUMMARIZED_AT = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)

# --- Who is alerted -------------------------------------------------------------------------------


class FakeSender:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, Alert]] = []

    def send(self, target: str, alert: Alert) -> None:
        self.alerts.append((target, alert))


@pytest.fixture
def call(clean_db: Engine) -> dict[str, int]:
    """Microsoft's call, summarized; subscriptions that should and shouldn't hear about it."""
    with session_factory(clean_db).begin() as session:
        session.add_all(
            [
                Company(cik=789019, ticker="MSFT", name="MICROSOFT CORP", on_watchlist=True),
                Company(cik=320193, ticker="AAPL", name="Apple Inc.", on_watchlist=True),
            ]
        )
        session.flush()
        transcript = CallTranscript(
            cik=789019,
            call_date=date(2026, 10, 28),
            source_url=SOURCE,
            text_sha256="x",
            words=9000,
            exchanges=7,
        )
        user = User(email="ops@example.com", google_sub="google-123")
        session.add_all([transcript, user])
        session.flush()
        summary = CallSummary(
            transcript_id=transcript.id,
            provider="ollama",
            model="llama3.2:3b",
            prompt_version=2,
            takeaways={
                "headline": "Microsoft on AI capacity",
                "summary": "Analysts asked about capacity and capital.",
                "key_points": ["Capacity constrained through the year"],
            },
            exchanges=[],
            ungrounded_numbers=[],
            ungrounded_names=[],
            created_at=SUMMARIZED_AT,
        )
        before = SUMMARIZED_AT - timedelta(days=1)

        def follow(
            cik: int | None, form_types: list[str], target: str, **more: Any
        ) -> Subscription:
            return Subscription(
                user_id=user.id,
                cik=cik,
                form_types=form_types,
                channel=Channel.DISCORD,
                target=f"https://discord.com/api/webhooks/1/{target}",
                created_at=more.pop("created_at", before),
                **more,
            )

        subscriptions = {
            "every_form": follow(789019, [], "every-form"),
            "calls_only": follow(789019, [CALL_QA], "calls-only"),
            "form4_only": follow(789019, ["4"], "form4-only"),
            "keyword": follow(789019, [], "keyword", query="Azure"),
            "too_new": follow(789019, [], "too-new", created_at=SUMMARIZED_AT + timedelta(hours=1)),
            "apple": follow(320193, [], "apple"),
        }
        session.add_all([summary, *subscriptions.values()])
        session.flush()
        return {"transcript": transcript.id, "summary": summary.id} | {
            name: s.id for name, s in subscriptions.items()
        }


def received(event: CallSummarized) -> Received:
    return Received(
        topic=Topic.CALLS_SUMMARIZED,
        partition=0,
        offset=0,
        key=event.key,
        value=event.model_dump_json().encode(),
        headers=(),
    )


def test_a_call_alerts_every_form_and_call_subscriptions_once(
    clean_db: Engine, call: dict[str, int]
) -> None:
    sender = FakeSender()
    handler = CallNotificationHandler(session_factory(clean_db), {Channel.DISCORD: sender})
    event = CallSummarized(transcript_id=call["transcript"], summary_id=call["summary"], cik=789019)
    handler(received(event))

    assert sorted(target.rsplit("/", 1)[1] for target, _ in sender.alerts) == [
        "calls-only",
        "every-form",
    ]  # not the Form 4 alert, the keyword alert, the newer one, or Apple's
    alert = sender.alerts[0][1]
    assert alert.headline == "Earnings call Q&A: Microsoft on AI capacity"
    assert (alert.url, alert.call_id, alert.form_type) == (SOURCE, call["transcript"], CALL_QA)
    assert alert.company_name == "Microsoft Corp"
    assert alert.reason is None  # a plain alert on the company: it says enough
    with clean_db.connect() as conn:
        rows = conn.execute(
            sa.select(Notification.transcript_id, Notification.accession_no, Notification.status)
        ).all()
    assert rows == [(call["transcript"], None, NotificationStatus.SENT)] * 2

    handler(received(event))  # a redelivery or replay
    assert len(sender.alerts) == 2


def test_the_notifier_hands_each_topic_to_its_handler(
    clean_db: Engine, call: dict[str, int]
) -> None:
    sender = FakeSender()
    calls = CallNotificationHandler(session_factory(clean_db), {Channel.DISCORD: sender})
    dispatch = TopicHandler({Topic.CALLS_SUMMARIZED: calls})
    event = CallSummarized(transcript_id=call["transcript"], summary_id=call["summary"], cik=789019)
    dispatch(received(event))
    assert len(sender.alerts) == 2
    with pytest.raises(PermanentError, match="no handler"):
        dispatch(replace(received(event), topic="elsewhere"))


def test_an_unknown_summary_or_a_bad_event_is_a_dead_letter(
    clean_db: Engine, call: dict[str, int]
) -> None:
    handler = CallNotificationHandler(session_factory(clean_db), {Channel.DISCORD: FakeSender()})
    with pytest.raises(PermanentError, match="no summary"):
        handler(received(CallSummarized(transcript_id=1, summary_id=999, cik=789019)))
    with pytest.raises(PermanentError, match=r"invalid calls\.summarized"):
        handler(
            replace(received(CallSummarized(transcript_id=1, summary_id=1, cik=1)), value=b"{}")
        )
