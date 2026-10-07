"""Verifying Google ID tokens: the "Sign in with Google" credential the website posts to us.

An ID token is a JWT signed by Google with RS256. It is valid for this app when the signature
checks out against one of Google's published keys (its JWKS), `aud` is our client ID, `iss` is
Google, it hasn't expired, and Google has verified the email address.

Google's keys are fetched with httpx2 and cached in memory for as long as Google's Cache-Control
says (clamped to sane bounds). A token signed with a key we don't have triggers a refetch, since
Google rotates its keys, but at most once a minute, so junk tokens can't make the API hammer
Google. See https://developers.google.com/identity/gsi/web/guides/verify-google-id-token.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

logger = logging.getLogger(__name__)

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
# The only algorithm Google signs ID tokens with. Never "none", never HS256: with HS256, a token
# "signed" with our public key (which anyone can download) would verify.
ALGORITHM = "RS256"
# Clock skew tolerated on `exp` and `iat`.
LEEWAY_SECONDS = 30
# How long fetched keys are used: Google's Cache-Control max-age, within these bounds.
MIN_CACHE_SECONDS = 300
MAX_CACHE_SECONDS = 86_400
# At most one fetch per this many seconds, however many tokens name an unknown key.
MIN_REFETCH_SECONDS = 60.0

_MAX_AGE = re.compile(r"max-age=(\d+)")


class InvalidIdTokenError(Exception):
    """Not a valid Google ID token for this app. The message says why, for the logs."""


class SigningKeysUnavailableError(Exception):
    """Google's signing keys couldn't be fetched and none are cached."""


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str  # Google's stable account ID; the email address can change
    email: str


class SigningKeys(Protocol):
    """Public keys by key ID (`kid`). Tests pass a plain dict instead of fetching Google's."""

    def get(self, kid: str, /) -> RSAPublicKey | None: ...


def _cache_seconds(cache_control: str | None) -> float:
    match = _MAX_AGE.search(cache_control or "")
    max_age = int(match.group(1)) if match else 0
    return min(max(max_age, MIN_CACHE_SECONDS), MAX_CACHE_SECONDS)


def _parse_jwks(document: object) -> dict[str, RSAPublicKey]:
    """The RS256 public keys in a JWKS document, by key ID. Other entries are skipped."""
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("not a JWKS document")
    keys: dict[str, RSAPublicKey] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(kid := entry.get("kid"), str):
            continue
        try:
            jwk = jwt.PyJWK(entry)
        except jwt.PyJWTError:
            continue
        if jwk.algorithm_name == ALGORITHM and isinstance(jwk.key, RSAPublicKey):
            keys[kid] = jwk.key
    if not keys:
        raise ValueError("JWKS has no usable RS256 keys")
    return keys


class GoogleSigningKeys:
    """Google's published signing keys, fetched on demand and cached in memory.

    If a refetch fails, the last good keys stay in use (Google keeps retired keys published for a
    while anyway); with no keys at all, `get` raises `SigningKeysUnavailableError`.
    """

    def __init__(
        self,
        *,
        url: str = GOOGLE_CERTS_URL,
        timeout: float = 5.0,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_refetch_seconds: float = MIN_REFETCH_SECONDS,
    ) -> None:
        self._url = url
        self._http = httpx2.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self._clock = clock
        self._min_refetch_seconds = min_refetch_seconds
        self._keys: dict[str, RSAPublicKey] | None = None
        self._expires_at = 0.0
        self._last_fetch: float | None = None
        # Requests run in a thread pool: one fetch at a time, and the others wait for its keys.
        self._lock = threading.Lock()

    def get(self, kid: str, /) -> RSAPublicKey | None:
        with self._lock:
            now = self._clock()
            stale = self._keys is None or now >= self._expires_at or kid not in self._keys
            throttled = (
                self._last_fetch is not None and now - self._last_fetch < self._min_refetch_seconds
            )
            if stale and not throttled:
                self._fetch(now)
            if self._keys is None:
                raise SigningKeysUnavailableError("Google's signing keys could not be fetched")
            return self._keys.get(kid)

    def _fetch(self, now: float) -> None:
        self._last_fetch = now
        try:
            response = self._http.get(self._url)
            response.raise_for_status()
            keys = _parse_jwks(response.json())
        except (httpx2.HTTPError, ValueError) as exc:
            logger.warning("couldn't fetch Google's signing keys: %r", exc)
            return
        self._keys = keys
        self._expires_at = now + _cache_seconds(response.headers.get("Cache-Control"))
        logger.info("fetched %d Google signing key(s)", len(keys))

    def close(self) -> None:
        self._http.close()


class GoogleIdTokenVerifier:
    def __init__(
        self, client_id: str, keys: SigningKeys, *, leeway: float = LEEWAY_SECONDS
    ) -> None:
        self._client_id = client_id
        self._keys = keys
        self._leeway = leeway

    def verify(self, token: str) -> GoogleIdentity:
        """The token's Google account, or `InvalidIdTokenError`. Can also raise
        `SigningKeysUnavailableError`: that's our problem, not the token's."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidIdTokenError(f"malformed token: {exc}") from exc
        # Checked before looking up the key, so tokens that can never verify don't cause fetches.
        if header.get("alg") != ALGORITHM:
            raise InvalidIdTokenError("not signed with RS256")
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise InvalidIdTokenError("no key ID")
        key = self._keys.get(kid)
        if key is None:
            raise InvalidIdTokenError("signed with an unknown key")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._client_id,
                issuer=GOOGLE_ISSUERS,
                leeway=self._leeway,
                options={
                    "require": ["iss", "aud", "exp", "iat", "sub", "email"],
                    "strict_aud": True,
                    "enforce_minimum_key_length": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidIdTokenError(f"{exc.__class__.__name__}: {exc}") from exc
        sub, email = claims["sub"], claims["email"]
        if not (isinstance(sub, str) and sub and isinstance(email, str) and email):
            raise InvalidIdTokenError("sub or email is not a non-empty string")
        # Google sends a JSON boolean. An unverified address could belong to someone else.
        if claims.get("email_verified") is not True:
            raise InvalidIdTokenError("email address not verified by Google")
        return GoogleIdentity(sub=sub, email=email)
