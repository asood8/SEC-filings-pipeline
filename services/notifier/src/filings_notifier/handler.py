"""Handle one `filings.ready` event: alert every matching subscription, once.

- A subscription matches if it follows the filing's company, or, for a Form 4, the issuer whose
  stock was traded (following Lennar catches Berkshire's Form 4 about buying Lennar), and if its
  form types include the filing's (empty means all).
- Alerts are about new filings: a subscription only matches filings discovered after it was
  created. Otherwise replaying the topic would send a new subscription every past filing.
- Each alert is a `notifications` row, unique per (subscription, filing). It is delivered inside
  a transaction that locks the row, and skipped unless still pending, so redeliveries, replays,
  and concurrent notifiers never alert a subscription twice about the same filing.
- A permanent failure (deleted webhook) marks that alert failed and moves on. A transient failure
  leaves it pending and, after the other subscriptions are handled, raises so the consume loop
  retries; the retry delivers only what is still pending.
- Once every alert is settled, the filing moves ready → notified.

A crash between a successful send and the commit can resend that one alert; webhooks offer no
idempotency keys, so that tiny window can't be closed.

Each delivery is a span with only the channel and the outcome: never the target (a secret webhook
URL or a personal email address), and errors by type only, since their messages can quote it. The
`filings.notifications` metric counts deliveries by the same channel and outcome.
"""

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from opentelemetry import trace
from opentelemetry.trace import Span
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from filings_common import metrics, telemetry
from filings_common.alert_filters import OPEN_MARKET_CODES, TradeLine, wants_8k, wants_form4
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
)
from filings_common.events import FilingReady, Topic
from filings_common.names import company_name
from filings_common.parsed import InvestorCall
from filings_common.summaries import FilingSummary
from filings_common.targets import target_is_valid
from filings_notifier.channels import Alert, DeliveryError, PermanentDeliveryError, Sender

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

DeliveryOutcome = Literal["sent", "failed", "retry", "already_settled"]


class PendingAlertsError(Exception):
    """Some alerts couldn't be delivered yet; the consume loop retries the event."""


class NotificationHandler:
    def __init__(self, sessions: sessionmaker[Session], senders: Mapping[Channel, Sender]) -> None:
        self._sessions = sessions
        self._senders = senders
        metrics.start_at_zero(
            metrics.NOTIFICATIONS,
            (
                {metrics.ALERT_CHANNEL: channel.value, telemetry.OUTCOME: outcome}
                for channel in senders
                for outcome in metrics.NOTIFICATION_OUTCOMES
            ),
        )

    def __call__(self, received: Received) -> None:
        try:
            event = FilingReady.model_validate_json(received.value)
        except ValidationError as exc:
            raise PermanentError(f"invalid {Topic.FILINGS_READY} event: {exc}") from exc
        telemetry.tag_filing(event.accession_no, form_type=event.form_type, cik=event.cik)

        with self._sessions() as session:
            alert, discovered_at = self._alert(session, event)
            subscription_ids = self._matching_subscriptions(
                session, alert, event.cik, discovered_at
            )

        pending = [sid for sid in subscription_ids if not self._deliver(sid, alert)]
        if pending:
            raise PendingAlertsError(
                f"{len(pending)} alert(s) for {event.accession_no} not delivered yet"
            )
        with self._sessions.begin() as session:
            session.execute(
                sa.update(Filing)
                .where(
                    Filing.accession_no == event.accession_no, Filing.status == FilingStatus.READY
                )
                .values(status=FilingStatus.NOTIFIED, updated_at=sa.func.now())
            )
        logger.info("%s: %d subscription(s) alerted", event.accession_no, len(subscription_ids))

    def _alert(self, session: Session, event: FilingReady) -> tuple[Alert, datetime]:
        """The alert to send, and when the filing was discovered."""
        row = session.execute(
            sa.select(Filing, Company)
            .join(Company, Company.cik == Filing.cik)
            .where(Filing.accession_no == event.accession_no)
        ).one_or_none()
        summary = session.get(Summary, event.summary_id)
        if row is None or summary is None:
            raise PermanentError(f"no filing or summary for {event.accession_no}")
        filing, company = row
        content = FilingSummary.model_validate(summary.summary)
        form8k = session.get(Form8KFiling, filing.accession_no)  # None unless an 8-K
        alert = Alert(
            accession_no=filing.accession_no,
            company_name=company_name(company.name),
            ticker=company.ticker,
            form_type=filing.form_type,
            filed_at=filing.filed_at,
            headline=content.headline,
            summary=content.summary,
            key_points=content.key_points,
            url=filing.primary_doc_url,
            investor_call=(
                InvestorCall.model_validate(form8k.investor_call)
                if form8k is not None and form8k.investor_call is not None
                else None
            ),
        )
        return alert, filing.created_at

    def _matching_subscriptions(
        self, session: Session, alert: Alert, cik: int, discovered_at: datetime
    ) -> list[int]:
        ciks = {cik}
        form4 = session.get(Form4Filing, alert.accession_no)
        if form4 is not None:
            ciks.add(form4.issuer_cik)
        query = (
            sa.select(
                Subscription.id,
                Subscription.form4_trades,
                Subscription.form4_min_value,
                Subscription.form8k_items,
            )
            .where(
                Subscription.cik.in_(ciks),
                Subscription.created_at <= discovered_at,
                # No form types means all of them; otherwise the filing's must be listed.
                (sa.func.cardinality(Subscription.form_types) == 0)
                | Subscription.form_types.contains([alert.form_type]),
            )
            .order_by(Subscription.id)
        )
        rows = session.execute(query).all()
        if form4 is None:
            # 8-K item filters (filings_common.alert_filters); other forms have no items.
            form8k = session.get(Form8KFiling, alert.accession_no)
            filing_items = form8k.items if form8k is not None else []
            return [
                row.id for row in rows if form8k is None or wants_8k(row.form8k_items, filing_items)
            ]
        # Form 4 filters (filings_common.alert_filters): read the trade lines once, if needed.
        lines: list[TradeLine] | None = None
        wanted = []
        for row in rows:
            if row.form4_trades != "all" or row.form4_min_value is not None:
                if lines is None:
                    lines = _trade_lines(session, alert.accession_no)
                if not wants_form4(row.form4_trades, row.form4_min_value, lines):
                    continue
            wanted.append(row.id)
        return wanted

    def _deliver(self, subscription_id: int, alert: Alert) -> bool:
        """Deliver one alert. True when settled (sent, or failed for good); False to retry."""
        with telemetry.redacted_span(tracer, "deliver alert") as span:
            outcome, channel = self._deliver_once(subscription_id, alert, span)
            span.set_attribute(telemetry.OUTCOME, outcome)
        if channel is not None:  # counted once committed; already settled alerts aren't counted
            metrics.NOTIFICATIONS.add(
                1, {metrics.ALERT_CHANNEL: channel.value, telemetry.OUTCOME: outcome}
            )
        return outcome != "retry"

    def _deliver_once(
        self, subscription_id: int, alert: Alert, span: Span
    ) -> tuple[DeliveryOutcome, Channel | None]:
        """Deliver the alert unless it is already settled. Returns the outcome (sent, failed for
        good, retry after a transient failure, or already_settled) and the channel tried, if
        any."""
        with self._sessions.begin() as session:
            session.execute(
                pg_insert(Notification)
                .values(subscription_id=subscription_id, accession_no=alert.accession_no)
                .on_conflict_do_nothing(index_elements=["subscription_id", "accession_no"])
            )
            notification = session.execute(
                sa.select(Notification)
                .where(
                    Notification.subscription_id == subscription_id,
                    Notification.accession_no == alert.accession_no,
                )
                .with_for_update()
            ).scalar_one()
            if notification.status is not NotificationStatus.PENDING:
                return "already_settled", None  # sent, or failed for good: never alert twice
            subscription = session.get_one(Subscription, subscription_id)
            channel = subscription.channel
            span.set_attribute(metrics.ALERT_CHANNEL, channel.value)
            notification.attempts += 1
            try:
                if not target_is_valid(subscription.channel, subscription.target):
                    raise PermanentDeliveryError(f"invalid {subscription.channel} target")
                self._senders[subscription.channel].send(subscription.target, alert)
            except PermanentDeliveryError as exc:
                notification.status = NotificationStatus.FAILED
                notification.last_error = str(exc)[:500]
                logger.warning("alert %d failed permanently: %s", notification.id, exc)
                telemetry.record_error(span, exc, redact=True)
                return "failed", channel
            except DeliveryError as exc:
                notification.last_error = str(exc)[:500]
                logger.warning("alert %d not delivered yet: %s", notification.id, exc)
                telemetry.record_error(span, exc, redact=True)
                return "retry", channel
            notification.status = NotificationStatus.SENT
            notification.sent_at = sa.func.now()
            notification.last_error = None
            return "sent", channel


def _trade_lines(session: Session, accession_no: str) -> list[TradeLine]:
    """A Form 4's open-market stock trades, the only lines the filters look at."""
    rows = session.execute(
        sa.select(
            Form4Transaction.code, Form4Transaction.shares, Form4Transaction.price_per_share
        ).where(
            Form4Transaction.accession_no == accession_no,
            Form4Transaction.is_derivative.is_(False),
            Form4Transaction.code.in_(sorted(OPEN_MARKET_CODES)),
        )
    )
    return [TradeLine(row.code, row.shares, row.price_per_share) for row in rows]
