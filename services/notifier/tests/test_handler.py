from datetime import UTC, date, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from sqlalchemy.engine import Engine

from filings_common.consumer import PermanentError, Received
from filings_common.db import (
    Channel,
    Company,
    Filing,
    FilingStatus,
    Form4Filing,
    Form4Transaction,
    Form8KFiling,
    Notification,
    NotificationStatus,
    Subscription,
    Summary,
    User,
    session_factory,
)
from filings_common.events import FilingReady, Topic
from filings_common.parsed import InvestorCall
from filings_notifier.channels import Alert, DeliveryError, PermanentDeliveryError
from filings_notifier.handler import NotificationHandler, PendingAlertsError

pytestmark = pytest.mark.integration

FORM4 = "0001193125-26-397059"  # Berkshire's Form 4 about buying Lennar
DISCORD = "https://discord.com/api/webhooks/1/apple-hook"
SLACK = "https://hooks.slack.com/services/T1/B1/applehook"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.alerts: list[Alert] = []
        self.failures: list[Exception] = []  # raised (in order) before succeeding

    def send(self, target: str, alert: Alert) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((target, alert.accession_no))
        self.alerts.append(alert)


@pytest.fixture
def world(clean_db: Engine) -> dict[str, int]:
    """Berkshire filed a Form 4 about buying Lennar stock. Subscriptions follow each company."""
    with session_factory(clean_db).begin() as session:
        session.add_all(
            [
                Company(
                    cik=1067983, ticker="BRK-B", name="BERKSHIRE HATHAWAY INC", on_watchlist=True
                ),
                Company(cik=920760, ticker="LEN", name="LENNAR CORP", on_watchlist=False),
                Company(cik=320193, ticker="AAPL", name="Apple Inc.", on_watchlist=True),
            ]
        )
        session.flush()
        session.add(
            Filing(
                accession_no=FORM4,
                cik=1067983,
                form_type="4",
                filed_at=date(2026, 9, 19),
                primary_doc_url="https://www.sec.gov/x.xml",
                raw_object_key="filings/x.xml",
                status=FilingStatus.READY,
            )
        )
        session.flush()
        summary = Summary(
            accession_no=FORM4,
            provider="template",
            model="form4-template",
            prompt_version=1,
            summary={
                "headline": "Berkshire bought LEN shares",
                "summary": "Insider purchase.",
                "key_points": [],
            },
        )
        user = User(email="ops@example.com", google_sub="google-123")
        other = User(email="analyst@example.com", google_sub="google-456")
        session.add_all(
            [
                summary,
                user,
                other,
                Form4Filing(
                    accession_no=FORM4,
                    period_of_report=date(2026, 9, 17),
                    issuer_cik=920760,
                    issuer_name="LENNAR CORP",
                    issuer_ticker="LEN",
                    reporting_owners=["BERKSHIRE HATHAWAY INC"],
                    transaction_codes=["P"],
                    is_10b5_1=False,
                ),
            ]
        )
        session.flush()
        subs = {
            "berkshire_discord": Subscription(
                user_id=user.id, cik=1067983, form_types=[], channel=Channel.DISCORD, target=DISCORD
            ),
            "lennar_slack": Subscription(
                user_id=user.id, cik=920760, form_types=["4"], channel=Channel.SLACK, target=SLACK
            ),
            "berkshire_email": Subscription(
                user_id=user.id,
                cik=1067983,
                form_types=[],
                channel=Channel.EMAIL,
                target="ops@example.com",
            ),
            "berkshire_8k_only": Subscription(
                user_id=other.id,
                cik=1067983,
                form_types=["8-K"],
                channel=Channel.EMAIL,
                target="analyst@example.com",
            ),
            "apple": Subscription(
                user_id=user.id,
                cik=320193,
                form_types=[],
                channel=Channel.EMAIL,
                target="ops@example.com",
            ),
            "bad_target": Subscription(
                user_id=user.id,
                cik=1067983,
                form_types=[],
                channel=Channel.DISCORD,
                target="https://evil.example/hook",
            ),
        }
        session.add_all(subs.values())
        session.flush()
        return {"summary_id": summary.id} | {name: s.id for name, s in subs.items()}


def ready_event(summary_id: int) -> Received:
    event = FilingReady(accession_no=FORM4, cik=1067983, form_type="4", summary_id=summary_id)
    return Received(Topic.FILINGS_READY, 0, 0, FORM4, event.model_dump_json().encode())


def notifications(engine: Engine) -> dict[int, tuple[NotificationStatus, int]]:
    with session_factory(engine)() as session:
        rows = session.scalars(sa.select(Notification)).all()
        return {n.subscription_id: (n.status, n.attempts) for n in rows}


def filing_status(engine: Engine) -> FilingStatus:
    with session_factory(engine)() as session:
        return session.get_one(Filing, FORM4).status


def senders() -> dict[Channel, FakeSender]:
    return {Channel.DISCORD: FakeSender(), Channel.SLACK: FakeSender(), Channel.EMAIL: FakeSender()}


def test_alerts_each_matching_subscription_exactly_once(
    clean_db: Engine, world: dict[str, int]
) -> None:
    fakes = senders()
    handler = NotificationHandler(session_factory(clean_db), fakes)
    handler(ready_event(world["summary_id"]))
    handler(ready_event(world["summary_id"]))  # redelivery or replay

    assert fakes[Channel.DISCORD].sent == [(DISCORD, FORM4)]
    assert fakes[Channel.SLACK].sent == [(SLACK, FORM4)]  # follows the issuer, Lennar
    assert fakes[Channel.EMAIL].sent == [("ops@example.com", FORM4)]  # not the 8-K-only one
    states = notifications(clean_db)
    assert states[world["berkshire_discord"]] == (NotificationStatus.SENT, 1)
    assert states[world["bad_target"]][0] is NotificationStatus.FAILED  # never requested
    assert world["berkshire_8k_only"] not in states
    assert world["apple"] not in states
    assert filing_status(clean_db) is FilingStatus.NOTIFIED


def test_transient_failures_retry_only_what_is_pending(
    clean_db: Engine, world: dict[str, int]
) -> None:
    fakes = senders()
    fakes[Channel.SLACK].failures = [DeliveryError("Slack returned 503")]
    handler = NotificationHandler(session_factory(clean_db), fakes)

    with pytest.raises(PendingAlertsError):
        handler(ready_event(world["summary_id"]))
    assert notifications(clean_db)[world["lennar_slack"]] == (NotificationStatus.PENDING, 1)
    assert filing_status(clean_db) is FilingStatus.READY  # not every alert is settled yet

    handler(ready_event(world["summary_id"]))  # the consume loop's retry
    assert len(fakes[Channel.DISCORD].sent) == 1  # not resent
    assert fakes[Channel.SLACK].sent == [(SLACK, FORM4)]
    assert notifications(clean_db)[world["lennar_slack"]] == (NotificationStatus.SENT, 2)
    assert filing_status(clean_db) is FilingStatus.NOTIFIED


def test_permanent_failures_are_not_retried(clean_db: Engine, world: dict[str, int]) -> None:
    fakes = senders()
    fakes[Channel.DISCORD].failures = [PermanentDeliveryError("Unknown Webhook")]
    handler = NotificationHandler(session_factory(clean_db), fakes)
    handler(ready_event(world["summary_id"]))  # no exception: nothing to retry
    handler(ready_event(world["summary_id"]))
    assert notifications(clean_db)[world["berkshire_discord"]] == (NotificationStatus.FAILED, 1)
    assert fakes[Channel.DISCORD].sent == []
    assert filing_status(clean_db) is FilingStatus.NOTIFIED


def test_unknown_filing_is_a_permanent_error(clean_db: Engine, world: dict[str, int]) -> None:
    handler = NotificationHandler(session_factory(clean_db), senders())
    with pytest.raises(PermanentError):
        handler(ready_event(world["summary_id"] + 999))
    with pytest.raises(PermanentError):
        handler(Received(Topic.FILINGS_READY, 0, 0, FORM4, b"not json"))


def test_subscriptions_are_only_alerted_about_filings_found_after_them(
    clean_db: Engine, world: dict[str, int]
) -> None:
    """Replaying the topic must not send a newer subscription every past filing."""
    with session_factory(clean_db).begin() as session:
        user = session.scalars(sa.select(User).where(User.google_sub == "google-123")).one()
        late = Subscription(
            user_id=user.id,
            cik=1067983,
            form_types=[],
            channel=Channel.DISCORD,
            target="https://discord.com/api/webhooks/2/late-hook",
            created_at=sa.func.now() + sa.text("interval '1 minute'"),
        )
        session.add(late)
        session.flush()
        late_id = late.id
    fakes = senders()
    NotificationHandler(session_factory(clean_db), fakes)(ready_event(world["summary_id"]))
    assert late_id not in notifications(clean_db)
    assert (DISCORD, FORM4) in fakes[Channel.DISCORD].sent  # the earlier one still is


def test_delivery_spans_name_the_channel_and_outcome_but_never_the_target(
    clean_db: Engine, world: dict[str, int], spans: InMemorySpanExporter
) -> None:
    fakes = senders()
    # A transient error whose message quotes the webhook URL, as an HTTP client's might.
    fakes[Channel.SLACK].failures = [DeliveryError(f"Slack unreachable: POST {SLACK}")]
    handler = NotificationHandler(session_factory(clean_db), fakes)
    with pytest.raises(PendingAlertsError):
        handler(ready_event(world["summary_id"]))
    handler(ready_event(world["summary_id"]))  # the retry

    deliveries = [s for s in spans.get_finished_spans() if s.name == "deliver alert"]
    assert [dict(s.attributes or {}) for s in deliveries] == [
        {"filings.alert.channel": "discord", "filings.outcome": "sent"},
        {"filings.alert.channel": "slack", "filings.outcome": "retry"},
        {"filings.alert.channel": "email", "filings.outcome": "sent"},
        {"filings.alert.channel": "discord", "filings.outcome": "failed"},  # the invalid target
        {"filings.outcome": "already_settled"},
        {"filings.alert.channel": "slack", "filings.outcome": "sent"},
        {"filings.outcome": "already_settled"},
        {"filings.outcome": "already_settled"},
    ]
    assert deliveries[1].status.status_code is StatusCode.ERROR
    assert deliveries[1].status.description == "filings_notifier.channels.DeliveryError"
    for span in spans.get_finished_spans():
        recorded = span.to_json()
        for private in ("discord.com", "hooks.slack.com", "@example.com", "evil.example"):
            assert private not in recorded


def test_deliveries_are_counted_by_channel_and_outcome(
    clean_db: Engine, world: dict[str, int], measured: Any
) -> None:
    fakes = senders()
    fakes[Channel.SLACK].failures = [DeliveryError("Slack returned 503")]
    handler = NotificationHandler(session_factory(clean_db), fakes)
    # Every channel and outcome starts at zero, so the first of each shows as an increase.
    assert len(measured.attribute_sets("filings.notifications")) == 9
    with pytest.raises(PendingAlertsError):
        handler(ready_event(world["summary_id"]))
    handler(ready_event(world["summary_id"]))  # the retry; already settled alerts don't count

    def counted(channel: str, outcome: str) -> float:
        attributes = {"filings.alert.channel": channel, "filings.outcome": outcome}
        return float(measured.total("filings.notifications", attributes))

    assert counted("discord", "sent") == 1
    assert counted("discord", "failed") == 1  # the invalid target
    assert counted("slack", "retry") == 1
    assert counted("slack", "sent") == 1
    assert counted("email", "sent") == 1
    assert measured.total("filings.notifications") == 5


def test_an_8k_alert_carries_the_call_it_announces(clean_db: Engine, world: dict[str, int]) -> None:
    accession_no = "0000320193-26-000090"
    call = {
        "kind": "Earnings call",
        "day": "2026-10-29",
        "starts_at": "2026-10-29T14:00:00-07:00",
        "webcast_url": "https://investor.apple.com",
    }
    with session_factory(clean_db).begin() as session:
        session.add(
            Filing(
                accession_no=accession_no,
                cik=320193,
                form_type="8-K",
                filed_at=date(2026, 10, 1),
                primary_doc_url="https://www.sec.gov/aapl.htm",
                raw_object_key="filings/aapl.htm",
                status=FilingStatus.READY,
            )
        )
        session.flush()
        summary = Summary(
            accession_no=accession_no,
            provider="gemini",
            model="gemini-flash",
            prompt_version=1,
            summary={"headline": "Apple to report results", "summary": "...", "key_points": []},
        )
        session.add_all(
            [summary, Form8KFiling(accession_no=accession_no, items=["8.01"], investor_call=call)]
        )
        session.flush()
        summary_id = summary.id
    fakes = senders()
    event = FilingReady(
        accession_no=accession_no, cik=320193, form_type="8-K", summary_id=summary_id
    )
    NotificationHandler(session_factory(clean_db), fakes)(
        Received(Topic.FILINGS_READY, 0, 0, accession_no, event.model_dump_json().encode())
    )
    [alert] = fakes[Channel.EMAIL].alerts  # the Apple subscription
    assert alert.investor_call == InvestorCall(
        kind="Earnings call",
        day=date(2026, 10, 29),
        starts_at=datetime(2026, 10, 29, 21, 0, tzinfo=UTC),
        webcast_url="https://investor.apple.com",
    )


def test_form4_filters_choose_which_subscriptions_are_alerted(
    clean_db: Engine, world: dict[str, int]
) -> None:
    # Berkshire bought Lennar stock ($80,000) and had shares withheld for taxes.
    early = datetime(2026, 1, 1, tzinfo=UTC)  # before the filing was discovered
    hooks = {
        "purchases": "https://discord.com/api/webhooks/2/purchases",
        "over 50k": "https://discord.com/api/webhooks/3/over-50k",
        "over 100k": "https://discord.com/api/webhooks/4/over-100k",
    }
    with session_factory(clean_db).begin() as session:
        user_id = session.scalar(sa.select(User.id).where(User.email == "ops@example.com"))
        session.add_all(
            [
                Form4Transaction(
                    accession_no=FORM4,
                    line=1,
                    is_derivative=False,
                    security_title="Common Stock",
                    code="P",
                    shares=1_000,
                    price_per_share=80.0,
                    acquired_disposed="A",
                ),
                Form4Transaction(
                    accession_no=FORM4,
                    line=2,
                    is_derivative=False,
                    security_title="Common Stock",
                    code="F",
                    shares=50,
                    price_per_share=80.0,
                    acquired_disposed="D",
                ),
                Subscription(
                    user_id=user_id,
                    cik=920760,
                    form_types=["4"],
                    channel=Channel.DISCORD,
                    target=hooks["purchases"],
                    form4_trades="purchases",
                    created_at=early,
                ),
                Subscription(
                    user_id=user_id,
                    cik=920760,
                    form_types=["4"],
                    channel=Channel.DISCORD,
                    target=hooks["over 50k"],
                    form4_trades="open_market",
                    form4_min_value=50_000,
                    created_at=early,
                ),
                Subscription(
                    user_id=user_id,
                    cik=920760,
                    form_types=["4"],
                    channel=Channel.DISCORD,
                    target=hooks["over 100k"],
                    form4_min_value=100_000,
                    created_at=early,
                ),
            ]
        )
    fakes = senders()
    NotificationHandler(session_factory(clean_db), fakes)(ready_event(world["summary_id"]))
    sent = {target for target, _ in fakes[Channel.DISCORD].sent}
    assert hooks["purchases"] in sent
    assert hooks["over 50k"] in sent
    assert hooks["over 100k"] not in sent  # $80,000 is below its minimum
    assert DISCORD in sent  # the unfiltered subscription still gets it
