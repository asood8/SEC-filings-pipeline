"""Settings, read from environment variables and, in local development, a `.env` file.

Each concern has its own small settings class so a service only needs (and validates) the
configuration it actually uses.
"""

import re
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


class MongoSettings(BaseSettings):
    model_config = _config()

    # Atlas in production: mongodb+srv://user:password@cluster.example.mongodb.net/
    mongodb_url: SecretStr
    mongodb_database: str = "filings"


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


class LlmSettings(BaseSettings):
    """The local, OpenAI-compatible LLM server: Ollama in development, llama.cpp in production."""

    model_config = _config("LLM_")

    base_url: str = "http://localhost:11434/v1"
    # Unset means no local model: summaries come from the fallback provider only.
    model: str | None = None
    # Recorded with each summary as its provider.
    provider_name: str = "ollama"
    timeout_seconds: float = Field(default=180.0, gt=0)


class GeminiSettings(BaseSettings):
    """Google's Gemini API, free tier: the fallback LLM provider."""

    model_config = _config("GEMINI_")

    api_key: SecretStr | None = None
    model: str = "gemini-3.5-flash-lite"
    # Paced below the free tier's per-minute cap, shared by all summarizers through Redis.
    requests_per_minute: float = Field(default=10.0, gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0)

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class EmbeddingSettings(BaseSettings):
    """The embedding model behind semantic search (an OpenAI-compatible server such as Ollama)."""

    model_config = _config("EMBEDDING_")

    base_url: str = "http://localhost:11434/v1"
    # Unset means no embeddings: search falls back to keywords only.
    model: str | None = None
    # nomic-embed-text is trained with these task prefixes; other models may want none.
    document_prefix: str = "search_document: "
    query_prefix: str = "search_query: "
    # For documents (summarizer, `filings-index`), which can wait.
    timeout_seconds: float = Field(default=60.0, gt=0)
    # For search queries, which someone is waiting on: past this, the API answers with keyword
    # results (degraded) rather than slowly. A query takes well under a second on an idle model;
    # this covers waiting behind a document the model is busy embedding.
    query_timeout_seconds: float = Field(default=2.0, gt=0)


class ApiSettings(BaseSettings):
    model_config = _config("API_")

    # Per client, shared by all API replicas through Redis.
    rate_limit_per_minute: float = Field(default=60.0, gt=0)
    rate_limit_burst: int = Field(default=20, ge=1)
    # Responses. 0 turns caching off, also for query embeddings (kept 10 minutes otherwise).
    cache_ttl_seconds: int = Field(default=60, ge=0)
    # Behind Cloudflare Tunnel the client address is in CF-Connecting-IP. Only trust it when the
    # API is reachable through the tunnel alone, or clients could spoof it.
    trust_proxy_headers: bool = False
    # How long a sign-in lasts before the user has to sign in again.
    session_lifetime_days: int = Field(default=7, ge=1, le=90)
    # Browsers then send the session cookie over https only (and to http://localhost).
    cookie_secure: bool = True
    # The website's origins, allowed to call the API with the session cookie (CORS), as JSON in
    # the environment: API_CORS_ORIGINS='["https://app.example.com"]'.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("cors_origins")
    @classmethod
    def _require_exact_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            if not re.fullmatch(r"https?://[^/\s*]+", origin):
                raise ValueError(
                    f"{origin!r} is not an exact origin like https://app.example.com "
                    "(no wildcard, path, or trailing slash)"
                )
        return origins


class GoogleSignInSettings(BaseSettings):
    """Sign in with Google. The OAuth client ID is public (the website embeds it too); the API
    only verifies ID tokens, so it needs no client secret."""

    model_config = _config("GOOGLE_")

    # From the Google Cloud console. Unset means sign-in is disabled; the rest of the API works.
    client_id: str | None = None

    @field_validator("client_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class TelemetrySettings(BaseSettings):
    """OpenTelemetry traces and metrics, from the standard OTEL_* variables. Without an endpoint
    both are off.

    Read here rather than by the OpenTelemetry SDK itself so that, like every other setting, they
    can also come from `.env` in development.
    """

    model_config = _config("OTEL_")

    # Base URL of an OTLP/HTTP receiver; spans go to <endpoint>/v1/traces and metrics to
    # <endpoint>/v1/metrics. Grafana Cloud's is https://otlp-gateway-<zone>.grafana.net/otlp, the
    # local viewer's (`make obs-up`) http://localhost:4318. Unset means telemetry is off.
    exporter_otlp_endpoint: str | None = None
    # Headers sent with every export, as comma-separated key=value pairs with URL-encoded values.
    # Grafana Cloud: Authorization=Basic%20<base64 of instance-id:token>. A secret: never logged.
    exporter_otlp_headers: SecretStr | None = None
    # Replaces the name the service gives itself (its service.name).
    service_name: str | None = None
    # More resource attributes as key=value pairs, e.g. deployment.environment.name=production.
    resource_attributes: str | None = None
    # The standard switch to turn the SDK off without removing the rest of the configuration.
    sdk_disabled: bool = False
    # Per signal: "none" turns just that one off (e.g. metrics without traces).
    traces_exporter: Literal["otlp", "none"] = "otlp"
    metrics_exporter: Literal["otlp", "none"] = "otlp"
    # Milliseconds between metric exports (the SDK's default, a minute). Each process also exports
    # once more on the way out.
    metric_export_interval: int = Field(default=60_000, ge=1_000)

    @field_validator(
        "exporter_otlp_endpoint",
        "exporter_otlp_headers",
        "service_name",
        "resource_attributes",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


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
