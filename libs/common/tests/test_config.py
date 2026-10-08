import pytest
from pydantic import ValidationError

from filings_common.config import (
    ApiSettings,
    DatabaseSettings,
    EdgarSettings,
    EmbeddingSettings,
    GoogleSignInSettings,
)


def test_user_agent_names_the_app_and_contact() -> None:
    settings = EdgarSettings(_env_file=None, contact_email="ops@example.com")
    assert settings.user_agent == "sec-filings-pipeline ops@example.com"


def test_contact_email_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    with pytest.raises(ValidationError, match="contact_email"):
        EdgarSettings(_env_file=None)


@pytest.mark.parametrize("email", ["", "nobody", "a@b", "@example.com", "a b@example.com"])
def test_rejects_unusable_contact_email(email: str) -> None:
    with pytest.raises(ValidationError, match="email address"):
        EdgarSettings(_env_file=None, contact_email=email)


def test_rate_limit_cannot_exceed_sec_maximum() -> None:
    with pytest.raises(ValidationError, match="rate_limit_per_second"):
        EdgarSettings(_env_file=None, contact_email="ops@example.com", rate_limit_per_second=11)


def test_reads_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "env@example.com")
    monkeypatch.setenv("SEC_RATE_LIMIT_PER_SECOND", "2.5")
    settings = EdgarSettings(_env_file=None)
    assert settings.contact_email == "env@example.com"
    assert settings.rate_limit_per_second == 2.5


def test_database_url_is_hidden_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:hunter2@localhost/db")
    settings = DatabaseSettings(_env_file=None)
    assert "hunter2" not in repr(settings)
    assert "hunter2" in settings.database_url.get_secret_value()


def test_api_session_and_cors_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["API_SESSION_LIFETIME_DAYS", "API_COOKIE_SECURE", "API_CORS_ORIGINS"]:
        monkeypatch.delenv(name, raising=False)
    settings = ApiSettings(_env_file=None)
    assert settings.session_lifetime_days == 7
    assert settings.cookie_secure is True
    assert settings.cors_origins == ["http://localhost:3000"]


def test_cors_origins_are_read_as_a_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CORS_ORIGINS", '["https://app.example.com", "http://localhost:3000"]')
    settings = ApiSettings(_env_file=None)
    assert settings.cors_origins == ["https://app.example.com", "http://localhost:3000"]


@pytest.mark.parametrize(
    "origin",
    ["*", "https://*.example.com", "https://app.example.com/", "app.example.com", "ftp://x.y"],
)
def test_cors_origins_must_be_exact(origin: str) -> None:
    with pytest.raises(ValidationError, match="exact origin"):
        ApiSettings(_env_file=None, cors_origins=[origin])


def test_google_client_id_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "1234-abc.apps.googleusercontent.com")
    assert GoogleSignInSettings(_env_file=None).client_id == "1234-abc.apps.googleusercontent.com"
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "  ")
    assert GoogleSignInSettings(_env_file=None).client_id is None  # blank means sign-in is off


def test_queries_get_a_shorter_embedding_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["EMBEDDING_TIMEOUT_SECONDS", "EMBEDDING_QUERY_TIMEOUT_SECONDS"]:
        monkeypatch.delenv(name, raising=False)
    settings = EmbeddingSettings(_env_file=None)
    assert (settings.timeout_seconds, settings.query_timeout_seconds) == (60.0, 2.0)
    monkeypatch.setenv("EMBEDDING_QUERY_TIMEOUT_SECONDS", "0.5")
    assert EmbeddingSettings(_env_file=None).query_timeout_seconds == 0.5
