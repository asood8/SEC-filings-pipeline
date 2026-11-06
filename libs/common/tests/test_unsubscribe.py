import pytest
from pydantic import ValidationError

from filings_common.config import UnsubscribeSettings
from filings_common.unsubscribe import subscription_id, token

SECRET = "s" * 64


def test_a_token_names_its_subscription_only_under_its_secret() -> None:
    signed = token(42, SECRET)
    assert signed.startswith("42.")
    assert subscription_id(signed, SECRET) == 42
    assert subscription_id(signed, "t" * 64) is None  # another secret
    forged = f"43.{signed.split('.', 1)[1]}"
    assert subscription_id(forged, SECRET) is None  # another subscription, same signature


@pytest.mark.parametrize("value", ["", "42", "42.", "0.abc", "-1.x", "42." + "a" * 31, "x.y"])
def test_malformed_tokens_name_nothing(value: str) -> None:
    assert subscription_id(value, SECRET) is None


def test_the_secret_is_optional_but_long_when_set() -> None:
    assert UnsubscribeSettings(_env_file=None, secret="").secret is None
    with pytest.raises(ValidationError, match="at least 32"):
        UnsubscribeSettings(_env_file=None, secret="short")
