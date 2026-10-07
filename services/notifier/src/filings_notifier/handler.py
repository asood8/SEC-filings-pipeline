"""Handle one `filings.ready` event: alert every matching subscription, once.

- A subscription matches if it follows the filing's company, or, for a Form 4, the issuer whose
  stock was traded (following Lennar catches Berkshire's Form 4 about buying Lennar), and if its
  form types include the filing's (empty means all).
- Each alert is a `notifications` row, unique per (subscription, filing). It is delivered inside
  a transaction that locks the row, and skipped unless still pending, so redeliveries, replays,
  and concurrent notifiers never alert a subscription twice about the same filing.
- A permanent failure (deleted webhook) marks that alert failed and moves on. A transient failure
  leaves it pending and, after the other subscriptions are handled, raises so the consume loop
  retries; the retry delivers only what is still pending.
- Once every alert is settled, the filing moves ready → notified.

A crash between a successful send and the commit can resend that one alert; webhooks offer no
idempotency keys, so that tiny window can't be closed.
"""

import logging
from collections.abc import Mapping

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

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
)
from filings_common.events import FilingReady, Topic
from filings_common.summaries import FilingSummary
from filings_notifier.channels import (
    Alert,
    DeliveryError,
    PermanentDeliveryError,
    Sender,
    target_is_valid,
)

logger = logging.getLogger(__name__)


class PendingAlertsError(Exception):
    """Some alerts couldn't be delivered yet; the consume loop retries the event."""


class NotificationHandler:
    def __init__(self, sessions: sessionmaker[Session], senders: Mapping[Channel, Sender]) -> None:
        self._sessions = sessions
        self._senders = senders

    def __call__(self, received: Received) -> None:
        try:
            event = FilingReady.model_validate_json(received.value)
        except ValidationError as exc:
            raise PermanentError(f"invalid {Topic.FILINGS_READY} event: {exc}") from exc

        with self._sessions() as session:
            alert = self._alert(session, event)
            subscription_ids = self._matching_subscriptions(session, alert, event.cik)

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

    def _alert(self, session: Session, event: FilingReady) -> Alert:
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
        return Alert(
            accession_no=filing.accession_no,
            company_name=company.name,
            ticker=company.ticker,
            form_type=filing.form_type,
            filed_at=filing.filed_at,
            headline=content.headline,
            summary=content.summary,
            key_points=content.key_points,
            url=filing.primary_doc_url,
        )

    def _matching_subscriptions(self, session: Session, alert: Alert, cik: int) -> list[int]:
        ciks = {cik}
        form4 = session.get(Form4Filing, alert.accession_no)
        if form4 is not None:
            ciks.add(form4.issuer_cik)
        query = (
            sa.select(Subscription.id)
            .where(
                Subscription.cik.in_(ciks),
                # No form types means all of them; otherwise the filing's must be listed.
                (sa.func.cardinality(Subscription.form_types) == 0)
                | Subscription.form_types.contains([alert.form_type]),
            )
            .order_by(Subscription.id)
        )
        return list(session.scalars(query))

    def _deliver(self, subscription_id: int, alert: Alert) -> bool:
        """Deliver one alert. True when settled (sent, or failed for good); False to retry."""
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
                return True  # already sent, or failed for good: never alert twice
            subscription = session.get_one(Subscription, subscription_id)
            notification.attempts += 1
            try:
                if not target_is_valid(subscription.channel, subscription.target):
                    raise PermanentDeliveryError(f"invalid {subscription.channel} target")
                self._senders[subscription.channel].send(subscription.target, alert)
            except PermanentDeliveryError as exc:
                notification.status = NotificationStatus.FAILED
                notification.last_error = str(exc)[:500]
                logger.warning("alert %d failed permanently: %s", notification.id, exc)
                return True
            except DeliveryError as exc:
                notification.last_error = str(exc)[:500]
                logger.warning("alert %d not delivered yet: %s", notification.id, exc)
                return False
            notification.status = NotificationStatus.SENT
            notification.sent_at = sa.func.now()
            notification.last_error = None
            return True
