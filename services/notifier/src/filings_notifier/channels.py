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
import ssl
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Any, Protocol

import httpx2

from filings_common.db import Channel
from filings_common.parsed import InvestorCall
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
    # The upcoming call an 8-K announces: when, and the webcast link.
    investor_call: InvestorCall | None = None


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
                        *_discord_call_field(alert.investor_call),
                    ],
                    "footer": {"text": DISCLAIMER},
                }
            ],
        }
        _post(self._http, target, payload, "Discord")


def _discord_call_field(call: InvestorCall | None) -> list[dict[str, Any]]:
    if call is None:
        return []
    value = call.when() + (f"\n{call.webcast_url}" if call.webcast_url else "")
    return [{"name": call.kind, "value": value, "inline": False}]


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
            # The notification's text, also read as markup: escaped like the blocks.
            "text": f"[{alert.ticker}] {_slack_escape(alert.headline)} {alert.url}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*<{alert.url}|{_slack_escape(alert.headline)}>*\n"
                        f"{_slack_escape(alert.summary)}"
                        + (
                            f"\n{_slack_escape(alert.investor_call.describe())}"
                            if alert.investor_call
                            else ""
                        ),
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
    # One line: a line break would make the header invalid (FilingSummary already removes them).
    message["Subject"] = " ".join(f"[{alert.ticker}] {alert.headline}".split())
    points = "\n".join(f"- {p}" for p in alert.key_points)
    call = f"{alert.investor_call.describe()}\n\n" if alert.investor_call else ""
    message.set_content(
        f"{alert.headline}\n\n{alert.summary}\n\n{points}\n\n{call}"
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
                # A verifying context: smtplib's default accepts any certificate, which would hand
                # the credentials and the alert to anyone on the network path.
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self._username, self._password)
                smtp.send_message(_email_message(target, alert, self._sender))
        except smtplib.SMTPRecipientsRefused as exc:
            # The codes only: the refusal is keyed by the recipient's (personal) address.
            codes = ", ".join(str(code) for code, _ in exc.recipients.values())
            raise PermanentDeliveryError(f"recipient refused (SMTP {codes})") from exc
        except smtplib.SMTPResponseException as exc:
            if 500 <= exc.smtp_code < 600:
                raise PermanentDeliveryError(f"SMTP {exc.smtp_code}") from exc
            raise DeliveryError(f"SMTP {exc.smtp_code}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"SMTP failed: {exc!r}") from exc


def mask_email(address: str) -> str:
    """The address with all but the first character of its local part hidden: a***@example.com.
    Enough to tell recipients apart in a log without writing anyone's address into it."""
    local, _, domain = address.rpartition("@")
    return f"{local[:1]}***@{domain}"


class LogEmailSender:
    """Development stand-in: logs the email instead of sending it, with the recipient masked
    (logs are kept and shipped where personal addresses don't belong)."""

    def send(self, target: str, alert: Alert) -> None:
        if not target_is_valid(Channel.EMAIL, target):
            raise PermanentDeliveryError("not a valid email address")
        logger.info(
            "email (not sent: EMAIL_BACKEND=log) to %s about %s: %s",
            mask_email(target),
            alert.accession_no,
            alert.headline,
        )
