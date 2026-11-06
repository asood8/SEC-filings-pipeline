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
from filings_common.summaries import DISCLAIMER
from filings_common.targets import target_is_valid

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Transient: retrying later may succeed."""


class PermanentDeliveryError(DeliveryError):
    """The target rejected the alert; retrying won't help."""


@dataclass(frozen=True)
class Alert:
    """An alert about a filing, or about an earnings call's Q&A summary (`call_id` set; ADR-0022):
    then `form_type` is alert_filters.CALL_QA, `filed_at` the call's day, `url` the transcript
    on the company's site, and `accession_no` empty."""

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
    # Why this subscription sent it, when the alert alone doesn't say (alert_filters.alert_reason).
    reason: str | None = None
    call_id: int | None = None  # the transcript, for an earnings call's Q&A
    # The website's page for it, once the site has a public address (ALERTS_SITE_URL): the
    # alert's title links it, and `url` (the original) is linked beside it.
    page_url: str | None = None

    @property
    def link(self) -> str:
        """Where the alert's title points: the website's page, else the original."""
        return self.page_url or self.url

    @property
    def original_label(self) -> str:
        return "Full transcript" if self.call_id is not None else "Original filing"

    @property
    def subject(self) -> str:
        """What the alert is about, for logs: the accession number, or the call."""
        return self.accession_no if self.call_id is None else f"call {self.call_id}"


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
                    "url": alert.link,
                    "description": alert.summary[:4000],
                    "fields": [
                        {
                            "name": "Company",
                            "value": f"{alert.company_name} ({alert.ticker})",
                            "inline": True,
                        },
                        *_discord_what(alert),
                        *_discord_call_field(alert.investor_call),
                        *_discord_original_field(alert),
                    ],
                    "footer": {"text": _with_reason(DISCLAIMER, alert.reason)},
                }
            ],
        }
        _post(self._http, target, payload, "Discord")


def _discord_what(alert: Alert) -> list[dict[str, Any]]:
    if alert.call_id is not None:
        return [{"name": "Earnings call", "value": alert.filed_at.isoformat(), "inline": True}]
    return [
        {"name": "Form", "value": alert.form_type, "inline": True},
        {"name": "Filed", "value": alert.filed_at.isoformat(), "inline": True},
    ]


def _discord_original_field(alert: Alert) -> list[dict[str, Any]]:
    """With the title on the website's page, the original (sec.gov, the transcript) linked too."""
    if alert.page_url is None:
        return []
    return [{"name": alert.original_label, "value": alert.url, "inline": False}]


def _what(alert: Alert) -> str:
    """The alert's subject in words: "Form 8-K · filed 2026-10-13", or the earnings call's day."""
    if alert.call_id is not None:
        return f"earnings call of {alert.filed_at.isoformat()}"
    return f"Form {alert.form_type} · filed {alert.filed_at.isoformat()}"


def _with_reason(text: str, reason: str | None) -> str:
    return f"{reason} \u00b7 {text}" if reason else text


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
            f"{alert.company_name} ({alert.ticker}) · {_what(alert)} · "
            f"{_with_reason(DISCLAIMER, alert.reason)}"
        )
        original = (
            [{"type": "mrkdwn", "text": f"<{alert.url}|{alert.original_label}>"}]
            if alert.page_url is not None
            else []
        )
        payload = {
            # The notification's text, also read as markup: escaped like the blocks.
            "text": f"[{alert.ticker}] {_slack_escape(alert.headline)} {alert.link}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*<{alert.link}|{_slack_escape(alert.headline)}>*\n"
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
                    "elements": [
                        {"type": "mrkdwn", "text": _slack_escape(context)},
                        *original,
                    ],
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
    if alert.call_id is not None:
        about = (
            f"{alert.company_name} ({alert.ticker}), earnings call of "
            f"{alert.filed_at.isoformat()}\nFull transcript, on the company's site: {alert.url}"
        )
    else:
        about = (
            f"{alert.company_name} ({alert.ticker}), Form {alert.form_type}, "
            f"filed {alert.filed_at.isoformat()}\nOriginal filing: {alert.url}"
        )
    if alert.page_url is not None:
        about = f"On the website: {alert.page_url}\n{about}"
    message.set_content(
        f"{alert.headline}\n\n{alert.summary}\n\n{points}\n\n{call}{about}\n\n"
        + (f"{alert.reason}\n" if alert.reason else "")
        + f"{DISCLAIMER}\n"
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
            alert.subject,
            alert.headline,
        )
