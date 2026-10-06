import pytest
from pydantic import ValidationError

from filings_common.config import DatabaseSettings, EdgarSettings


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
