from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.consumer import PermanentError, Received
from filings_common.db import (
    Channel,
    Company,
    Filing,
    FilingStatus,
    Form4Filing,
    Notification,
    NotificationStatus,
    Subscription,
    Summary,
    User,
    session_factory,
)
from filings_common.events import FilingReady, Topic
from filings_notifier.channels import Alert, DeliveryError, PermanentDeliveryError
from filings_notifier.handler import NotificationHandler, PendingAlertsError

pytestmark = pytest.mark.integration

FORM4 = "0001193125-26-397059"  # Berkshire's Form 4 about buying Lennar
DISCORD = "https://discord.com/api/webhooks/1/apple-hook"
SLACK = "https://hooks.slack.com/services/T1/B1/applehook"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failures: list[Exception] = []  # raised (in order) before succeeding

    def send(self, target: str, alert: Alert) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((target, alert.accession_no))


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
