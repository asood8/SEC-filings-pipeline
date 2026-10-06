"""Settings, read from environment variables and, in local development, a `.env` file.

Each concern has its own small settings class so a service only needs (and validates) the
configuration it actually uses.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _config(prefix: str = "") -> SettingsConfigDict:
    return SettingsConfigDict(env_prefix=prefix, env_file=".env", extra="ignore")


class EdgarSettings(BaseSettings):
    """SEC EDGAR access. SEC requires a User-Agent that names the app and gives a contact email."""

    model_config = _config("SEC_")

    contact_email: str
    app_name: str = "sec-filings-pipeline"
    # Shared by every worker through Redis. SEC's documented maximum is 10 requests/second.
    rate_limit_per_second: float = Field(default=5.0, gt=0, le=10)
    rate_limit_burst: int = Field(default=1, ge=1, le=5)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)

    @field_validator("contact_email")
    @classmethod
    def _require_email(cls, value: str) -> str:
        local, _, domain = value.partition("@")
        if not local or "." not in domain or any(c.isspace() for c in value):
            raise ValueError("must be an email address SEC can use to contact you")
        return value

    @property
    def user_agent(self) -> str:
        return f"{self.app_name} {self.contact_email}"


class DatabaseSettings(BaseSettings):
    model_config = _config()

    # SQLAlchemy URL, e.g. postgresql+psycopg://user:password@localhost:5432/filings
    database_url: SecretStr


class RedisSettings(BaseSettings):
    model_config = _config()

    redis_url: str = "redis://localhost:6379/0"


class KafkaSettings(BaseSettings):
    model_config = _config("KAFKA_")

    bootstrap_servers: str = "localhost:19092"
    # Used when the relay creates missing topics. One node in production, so one replica.
    topic_partitions: int = Field(default=3, ge=1)
    topic_replication_factor: int = Field(default=1, ge=1)


class OutboxRelaySettings(BaseSettings):
    model_config = _config("OUTBOX_RELAY_")

    batch_size: int = Field(default=100, ge=1)
    # How long to wait before polling again when the outbox is empty.
    idle_seconds: float = Field(default=1.0, gt=0)


class ObjectStoreSettings(BaseSettings):
    """Where raw filings are stored: a local directory in development, R2 (S3 API) in production."""

    model_config = _config("OBJECT_STORE_")

    backend: Literal["local", "s3"] = "local"
    local_root: Path = Path("data/objects")
    s3_bucket: str | None = None
    # R2: https://<account_id>.r2.cloudflarestorage.com
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
