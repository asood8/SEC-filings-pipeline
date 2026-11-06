"""Signed one-click unsubscribe tokens for email alerts (ADR-0012).

A token names one subscription and carries an HMAC of it under `UNSUBSCRIBE_SECRET`, so it can
only come from the notifier, and works without signing in: the email went only to the account's
own address. Tokens don't expire (an unsubscribe link should keep working); changing the secret
voids them all.
"""

import base64
import hashlib
import hmac
import re

_TOKEN = re.compile(r"^(?P<id>[1-9]\d{0,18})\.(?P<mac>[A-Za-z0-9_-]{32})$")


def _mac(subscription_id: int, secret: str) -> str:
    digest = hmac.new(
        secret.encode(), f"unsubscribe:{subscription_id}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode()[:32]  # 192 bits


def token(subscription_id: int, secret: str) -> str:
    return f"{subscription_id}.{_mac(subscription_id, secret)}"


def subscription_id(value: str, secret: str) -> int | None:
    """The subscription a token names, or None when it isn't one this secret signed."""
    match = _TOKEN.fullmatch(value)
    if match is None:
        return None
    subscription = int(match["id"])
    if not hmac.compare_digest(match["mac"], _mac(subscription, secret)):
        return None
    return subscription
