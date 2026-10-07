"""Alert delivery channels: Discord and Slack webhooks, and email.

Targets come from users. Every sender checks its target first with the shared validation in
`filings_common.targets` (official Discord or Slack webhook URLs only, email addresses without extra
headers), and redirects are never followed, so the notifier can't be pointed at internal addresses.

Failures are classified so retries stay sensible: `DeliveryError` is transient (rate limits,
server errors, network trouble: retry later); `PermanentDeliveryError` means the target rejected
the alert (a deleted webhook, a refused recipient: retrying would never help).
"""

import logging
import smtplib
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Any, Protocol

import httpx2

from filings_common.db import Channel
from filings_common.targets import target_is_valid

logger = logging.getLogger(__name__)

DISCLAIMER = "Automated summary; may contain errors and is not investment advice."


class DeliveryError(Exception):
    """Transient: retrying later may succeed."""


class PermanentDeliveryError(DeliveryError):
    """The target rejected the alert; retrying won't help."""


@dataclass(frozen=True)
class Alert:
    accession_no: str
    company_name: str
    ticker: str
    form_type: str
    filed_at: date
    headline: str
    summary: str
    key_points: list[str]
    url: str  # the filing on sec.gov


class Sender(Protocol):
    def send(self, target: str, alert: Alert) -> None: ...


def _post(http: httpx2.Client, url: str, payload: dict[str, Any], service: str) -> httpx2.Response:
    try:
        response = http.post(url, json=payload)
    except httpx2.TransportError as exc:
        raise DeliveryError(f"{service} unreachable: {exc!r}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise DeliveryError(f"{service} returned {response.status_code}")
    if not response.is_success:  # 4xx, or a redirect we deliberately don't follow
        raise PermanentDeliveryError(f"{service} rejected the alert: {response.status_code}")
    return response


class DiscordSender:
    def __init__(self, *, timeout: float = 10.0, transport: httpx2.BaseTransport | None = None):
        self._http = httpx2.Client(timeout=timeout, transport=transport, follow_redirects=False)

    def send(self, target: str, alert: Alert) -> None:
        if not target_is_valid(Channel.DISCORD, target):
            raise PermanentDeliveryError("not a Discord webhook URL")
        payload = {
            "username": "SEC Filings",
            # Never let summary text mention @everyone, roles, or users.
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": alert.headline[:256],
                    "url": alert.url,
                    "description": alert.summary[:4000],
                    "fields": [
                        {
                            "name": "Company",
                            "value": f"{alert.company_name} ({alert.ticker})",
                            "inline": True,
                        },
                        {"name": "Form", "value": alert.form_type, "inline": True},
                        {"name": "Filed", "value": alert.filed_at.isoformat(), "inline": True},
                    ],
                    "footer": {"text": DISCLAIMER},
                }
            ],
        }
        _post(self._http, target, payload, "Discord")


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackSender:
    def __init__(self, *, timeout: float = 10.0, transport: httpx2.BaseTransport | None = None):
        self._http = httpx2.Client(timeout=timeout, transport=transport, follow_redirects=False)

    def send(self, target: str, alert: Alert) -> None:
        if not target_is_valid(Channel.SLACK, target):
            raise PermanentDeliveryError("not a Slack webhook URL")
        context = (
            f"{alert.company_name} ({alert.ticker}) · Form {alert.form_type} · "
            f"filed {alert.filed_at.isoformat()} · {DISCLAIMER}"
        )
        payload = {
            "text": f"[{alert.ticker}] {alert.headline} {alert.url}",  # notification fallback
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*<{alert.url}|{_slack_escape(alert.headline)}>*\n"
                        f"{_slack_escape(alert.summary)}",
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": _slack_escape(context)}],
                },
            ],
        }
        _post(self._http, target, payload, "Slack")


def _email_message(target: str, alert: Alert, sender: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = target
    message["Subject"] = f"[{alert.ticker}] {alert.headline}"
    points = "\n".join(f"- {p}" for p in alert.key_points)
    message.set_content(
        f"{alert.headline}\n\n{alert.summary}\n\n{points}\n\n"
        f"{alert.company_name} ({alert.ticker}), Form {alert.form_type}, "
        f"filed {alert.filed_at.isoformat()}\nOriginal filing: {alert.url}\n\n{DISCLAIMER}\n"
    )
    return message


class SmtpEmailSender:
    """Email over SMTP with STARTTLS (Oracle Cloud Email Delivery in production)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        timeout: float = 20.0,
    ) -> None:
        self._host, self._port, self._timeout = host, port, timeout
        self._username, self._password, self._sender = username, password, sender

    def send(self, target: str, alert: Alert) -> None:
        if not target_is_valid(Channel.EMAIL, target):
            raise PermanentDeliveryError("not a valid email address")
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                smtp.starttls()
                smtp.login(self._username, self._password)
                smtp.send_message(_email_message(target, alert, self._sender))
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentDeliveryError(f"recipient refused: {exc.recipients}") from exc
        except smtplib.SMTPResponseException as exc:
            if 500 <= exc.smtp_code < 600:
                raise PermanentDeliveryError(f"SMTP {exc.smtp_code}") from exc
            raise DeliveryError(f"SMTP {exc.smtp_code}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"SMTP failed: {exc!r}") from exc


class LogEmailSender:
    """Development stand-in: logs the email instead of sending it."""

    def send(self, target: str, alert: Alert) -> None:
        if not target_is_valid(Channel.EMAIL, target):
            raise PermanentDeliveryError("not a valid email address")
        logger.info("email (not sent: EMAIL_BACKEND=log) to %s: %s", target, alert.headline)
