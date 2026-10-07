import json
import smtplib
from datetime import date
from typing import Any, ClassVar

import httpx2
import pytest

from filings_notifier.channels import (
    Alert,
    DeliveryError,
    DiscordSender,
    LogEmailSender,
    PermanentDeliveryError,
    SlackSender,
    SmtpEmailSender,
)

DISCORD = "https://discord.com/api/webhooks/123456/abc-DEF_789"
SLACK = "https://hooks.slack.com/services/T000/B000/XXXXyyyy1234"
ALERT = Alert(
    accession_no="0000320193-26-000001",
    company_name="Apple Inc.",
    ticker="AAPL",
    form_type="8-K",
    filed_at=date(2026, 9, 28),
    headline="Apple names <new> CFO & more @everyone",
    summary="Apple appointed a new CFO.",
    key_points=["Effective October 1"],
    url="https://www.sec.gov/Archives/x.htm",
)


def recorder(
    *responses: httpx2.Response | Exception,
) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []
    pending = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        response = pending.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx2.MockTransport(handler), requests


def test_discord_payload_never_mentions_anyone() -> None:
    transport, requests = recorder(httpx2.Response(204))
    DiscordSender(transport=transport).send(DISCORD, ALERT)
    body = json.loads(requests[0].content)
    assert body["allowed_mentions"] == {"parse": []}
    embed = body["embeds"][0]
    assert (embed["title"], embed["url"]) == (ALERT.headline, ALERT.url)
    assert {"name": "Form", "value": "8-K", "inline": True} in embed["fields"]


def test_slack_payload_escapes_markup() -> None:
    transport, requests = recorder(httpx2.Response(200, text="ok"))
    SlackSender(transport=transport).send(SLACK, ALERT)
    section = json.loads(requests[0].content)["blocks"][0]["text"]["text"]
    assert section.startswith(f"*<{ALERT.url}|Apple names &lt;new&gt; CFO &amp; more @everyone>*")


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx2.Response(429, json={"retry_after": 1.5}), DeliveryError),
        (httpx2.Response(503), DeliveryError),
        (httpx2.ConnectError("down"), DeliveryError),
        (httpx2.Response(404, json={"message": "Unknown Webhook"}), PermanentDeliveryError),
        (httpx2.Response(302, headers={"Location": "http://10.0.0.1/"}), PermanentDeliveryError),
    ],
)
def test_failures_are_classified(
    response: httpx2.Response | Exception, error: type[Exception]
) -> None:
    transport, requests = recorder(response)
    with pytest.raises(error) as excinfo:
        DiscordSender(transport=transport).send(DISCORD, ALERT)
    if error is DeliveryError:
        assert not isinstance(excinfo.value, PermanentDeliveryError)
    assert len(requests) == 1  # redirects are never followed


def test_invalid_targets_are_rejected_before_any_request() -> None:
    transport, requests = recorder()
    with pytest.raises(PermanentDeliveryError):
        SlackSender(transport=transport).send("https://example.com/hook", ALERT)
    with pytest.raises(PermanentDeliveryError):
        LogEmailSender().send("bad\naddress@example.com", ALERT)
    assert requests == []


class FakeSmtp:
    sent: ClassVar[list[Any]] = []
    refuse: ClassVar[bool] = False

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self) -> None:
        pass

    def login(self, username: str, password: str) -> None:
        pass

    def send_message(self, message: Any) -> None:
        if FakeSmtp.refuse:
            raise smtplib.SMTPRecipientsRefused({message["To"]: (550, b"no such user")})
        FakeSmtp.sent.append(message)


def test_smtp_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    FakeSmtp.sent, FakeSmtp.refuse = [], False
    sender = SmtpEmailSender(
        host="smtp", port=587, username="u", password="p", sender="alerts@example.com"
    )
    sender.send("ops@example.com", ALERT)
    [message] = FakeSmtp.sent
    assert message["Subject"] == f"[AAPL] {ALERT.headline}"
    assert ALERT.url in message.get_content()

    FakeSmtp.refuse = True
    with pytest.raises(PermanentDeliveryError, match="recipient refused"):
        sender.send("gone@example.com", ALERT)
