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
    timeout_seconds: float = Field(default=60.0, gt=0)


class ApiSettings(BaseSettings):
    model_config = _config("API_")

    # Per client, shared by all API replicas through Redis.
    rate_limit_per_minute: float = Field(default=60.0, gt=0)
    rate_limit_burst: int = Field(default=20, ge=1)
    cache_ttl_seconds: int = Field(default=60, ge=0)
    # Behind Cloudflare Tunnel the client address is in CF-Connecting-IP. Only trust it when the
    # API is reachable through the tunnel alone, or clients could spoof it.
    trust_proxy_headers: bool = False


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
