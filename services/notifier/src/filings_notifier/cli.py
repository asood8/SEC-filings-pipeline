"""The notifier process (`filings-notifier`): consume `filings.ready` until stopped."""

import logging
import signal
import threading
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from filings_common.config import DatabaseSettings, KafkaSettings
from filings_common.consumer import make_consumer, run_consumer
from filings_common.db import Channel, create_engine, session_factory
from filings_common.events import Topic
from filings_common.kafka import KafkaPublisher
from filings_notifier.channels import (
    DiscordSender,
    LogEmailSender,
    Sender,
    SlackSender,
    SmtpEmailSender,
)
from filings_notifier.handler import NotificationHandler

CONSUMER_GROUP = "notifier"
logger = logging.getLogger("filings_notifier")


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMAIL_", env_file=".env", extra="ignore")

    # "log" (development) logs emails instead of sending them.
    backend: Literal["log", "smtp"] = "log"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    sender: str | None = None


def email_sender(settings: EmailSettings) -> Sender:
    if settings.backend == "log":
        return LogEmailSender()
    if not (
        settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.sender
    ):
        raise ValueError(
            "EMAIL_BACKEND=smtp needs EMAIL_SMTP_HOST, _USERNAME, _PASSWORD, and EMAIL_SENDER"
        )
    return SmtpEmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password.get_secret_value(),
        sender=settings.sender,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    kafka = KafkaSettings()
    senders: dict[Channel, Sender] = {
        Channel.DISCORD: DiscordSender(),
        Channel.SLACK: SlackSender(),
        Channel.EMAIL: email_sender(EmailSettings()),
    }

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    dead_letters = KafkaPublisher(kafka.bootstrap_servers)
    try:
        run_consumer(
            make_consumer(kafka.bootstrap_servers, CONSUMER_GROUP),
            Topic.FILINGS_READY,
            NotificationHandler(session_factory(engine), senders),
            dead_letters,
            stop,
        )
    finally:
        dead_letters.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
