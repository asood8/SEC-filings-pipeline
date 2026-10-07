import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from filings_api.google import (
    GoogleIdentity,
    GoogleIdTokenVerifier,
    GoogleSigningKeys,
    InvalidIdTokenError,
    SigningKeysUnavailableError,
)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge(
    header: dict[str, Any],
    claims: dict[str, Any],
    sign: Callable[[bytes], bytes] | None = None,
) -> str:
    """A token built by hand, to get past the library's refusal to make bad ones."""
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}"
    signature = sign(signing_input.encode()) if sign else b""
    return f"{signing_input}.{b64url(signature)}"


def test_valid_token(google: Any) -> None:
    identity = google.verifier().verify(google.token())
    assert identity == GoogleIdentity(sub="100000000000000000001", email="ada@example.com")


@pytest.mark.parametrize("issuer", ["https://accounts.google.com", "accounts.google.com"])
def test_both_google_issuers_are_accepted(google: Any, issuer: str) -> None:
    assert google.verifier().verify(google.token(iss=issuer)).email == "ada@example.com"


def test_small_clock_skew_is_tolerated(google: Any) -> None:
    now = int(time.time())
    google.verifier().verify(google.token(exp=now - 10, iat=now + 10))


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"iat": int(time.time()) + 600}, "not yet valid"),
        ({"aud": "someone-else.apps.googleusercontent.com"}, "Audience"),
        ({"iss": "https://evil.example"}, "issuer"),
        ({"iss": "https://accounts.google.com.evil.example"}, "issuer"),
        ({"email_verified": False}, "not verified"),
        ({"email_verified": "true"}, "not verified"),
        ({"email_verified": None}, "not verified"),
        ({"exp": None}, "exp"),
        ({"iat": None}, "iat"),
        ({"sub": None}, "sub"),
        ({"email": None}, "email"),
        ({"email": ""}, "non-empty"),
    ],
)
def test_invalid_claims_are_rejected(google: Any, claims: dict[str, Any], reason: str) -> None:
    with pytest.raises(InvalidIdTokenError, match=reason):
        google.verifier().verify(google.token(**claims))


def test_token_for_several_audiences_is_rejected(google: Any) -> None:
    with pytest.raises(InvalidIdTokenError, match="Invalid claim format"):
        google.verifier().verify(google.token(aud=[google.client_id, "other-client"]))


def test_bad_signature_is_rejected(google: Any) -> None:
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidIdTokenError, match="Signature verification failed"):
        google.verifier().verify(google.token(key=impostor))


def test_tampered_claims_are_rejected(google: Any) -> None:
    header, _, signature = google.token().split(".")
    claims = b64url(json.dumps(google.claims(email="mallory@example.com")).encode())
    with pytest.raises(InvalidIdTokenError, match="Signature verification failed"):
        google.verifier().verify(f"{header}.{claims}.{signature}")


def test_unknown_key_id_is_rejected(google: Any) -> None:
    with pytest.raises(InvalidIdTokenError, match="unknown key"):
        google.verifier().verify(google.token(headers={"kid": "rotated-away"}))


def test_missing_key_id_is_rejected(google: Any) -> None:
    token = forge({"alg": "RS256", "typ": "JWT"}, google.claims())
    with pytest.raises(InvalidIdTokenError, match="no key ID"):
        google.verifier().verify(token)


def test_hs256_signed_with_the_public_key_is_rejected(google: Any) -> None:
    """Algorithm confusion: Google's public key is public, so an HMAC "signed" with it proves
    nothing. A verifier that let the token pick its algorithm would accept this."""
    public_pem = (
        google.key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .replace(b"\n", b"")
    )
    token = forge(
        {"alg": "HS256", "kid": google.kid},
        google.claims(),
        lambda data: hmac.new(public_pem, data, hashlib.sha256).digest(),
    )
    with pytest.raises(InvalidIdTokenError, match="not signed with RS256"):
        google.verifier().verify(token)


@pytest.mark.parametrize("alg", ["none", "None", "RS512", "ES256"])
def test_other_algorithms_are_rejected(google: Any, alg: str) -> None:
    token = forge({"alg": alg, "kid": google.kid}, google.claims())
    with pytest.raises(InvalidIdTokenError, match="not signed with RS256"):
        google.verifier().verify(token)


@pytest.mark.parametrize(
    "token",
    ["", "garbage", "a.b.c", "a.b", "e30.e30.", "!!!.???.###", "eyJhbGciOiJSUzI1NiJ9.x.y.z"],
)
def test_malformed_tokens_are_rejected(google: Any, token: str) -> None:
    with pytest.raises(InvalidIdTokenError):
        google.verifier().verify(token)


def test_garbage_payload_with_a_valid_header_is_rejected(google: Any) -> None:
    header = b64url(json.dumps({"alg": "RS256", "kid": google.kid}).encode())
    with pytest.raises(InvalidIdTokenError):
        google.verifier().verify(f"{header}.bm90IGpzb24.c2ln")


def test_error_messages_never_contain_the_token(google: Any) -> None:
    token = google.token(aud="someone-else")
    with pytest.raises(InvalidIdTokenError) as raised:
        google.verifier().verify(token)
    assert token not in str(raised.value)
    assert token.split(".")[1] not in str(raised.value)


# --- Google's signing keys (JWKS), fetched through a mock transport -------------------------


class FakeGoogle:
    """Serves a JWKS document and counts fetches. A fake clock drives cache expiry."""

    def __init__(self, *keys: rsa.RSAPrivateKey, cache_control: str = "max-age=1000") -> None:
        self.keys = {f"key-{i}": key for i, key in enumerate(keys)}
        self.cache_control = cache_control
        self.extra_jwks: list[Any] = []
        self.failure: httpx2.Response | Exception | None = None
        self.fetches = 0
        self.now = 0.0
        self.signing_keys = GoogleSigningKeys(
            transport=httpx2.MockTransport(self._handle), clock=lambda: self.now
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.fetches += 1
        assert str(request.url) == "https://www.googleapis.com/oauth2/v3/certs"
        if isinstance(self.failure, Exception):
            raise self.failure
        if self.failure is not None:
            return self.failure
        jwks = [
            {
                **RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
                "kid": kid,
                "alg": "RS256",
                "use": "sig",
            }
            for kid, key in self.keys.items()
        ]
        return httpx2.Response(
            200,
            json={"keys": jwks + self.extra_jwks},
            headers={"Cache-Control": f"public, {self.cache_control}, must-revalidate"},
        )


def test_keys_are_cached_for_max_age(google_key: rsa.RSAPrivateKey) -> None:
    fake = FakeGoogle(google_key, cache_control="max-age=1000")
    assert fake.signing_keys.get("key-0") is not None
    fake.now = 999
    assert fake.signing_keys.get("key-0") is not None
    assert fake.fetches == 1
    fake.now = 1000
    fake.signing_keys.get("key-0")
    assert fake.fetches == 2


@pytest.mark.parametrize(
    ("cache_control", "cached_for"),
    [("max-age=5", 300), ("max-age=99999999", 86_400), ("no-cache", 300)],
)
def test_cache_lifetime_is_clamped(
    google_key: rsa.RSAPrivateKey, cache_control: str, cached_for: float
) -> None:
    fake = FakeGoogle(google_key, cache_control=cache_control)
    fake.signing_keys.get("key-0")
    fake.now = cached_for - 1
    fake.signing_keys.get("key-0")
    assert fake.fetches == 1
    fake.now = cached_for
    fake.signing_keys.get("key-0")
    assert fake.fetches == 2


def test_unknown_key_refetches_at_most_once_a_minute(google_key: rsa.RSAPrivateKey) -> None:
    fake = FakeGoogle(google_key)
    fake.signing_keys.get("key-0")
    fake.now = 100
    for _ in range(50):  # a flood of tokens naming made-up keys
        assert fake.signing_keys.get("made-up") is None
    assert fake.fetches == 2  # the first fetch, plus one refetch
    fake.now = 159
    fake.signing_keys.get("made-up")
    assert fake.fetches == 2
    fake.now = 160
    fake.signing_keys.get("made-up")
    assert fake.fetches == 3


def test_rotated_key_is_found_by_refetching(google_key: rsa.RSAPrivateKey) -> None:
    fake = FakeGoogle(google_key)
    fake.signing_keys.get("key-0")
    fake.keys["key-new"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake.now = 61
    assert fake.signing_keys.get("key-new") is not None


def test_no_keys_at_all_is_unavailable(google_key: rsa.RSAPrivateKey) -> None:
    fake = FakeGoogle(google_key)
    fake.failure = httpx2.ConnectError("no route to host")
    with pytest.raises(SigningKeysUnavailableError):
        fake.signing_keys.get("key-0")
    with pytest.raises(SigningKeysUnavailableError):
        fake.signing_keys.get("key-0")
    assert fake.fetches == 1  # and it doesn't retry on every request either


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.Response(503),
        httpx2.Response(302, headers={"Location": "https://evil.example/certs"}),
        httpx2.Response(200, text="<html>not json</html>"),
        httpx2.Response(200, json={"keys": []}),
        httpx2.ReadTimeout("slow"),
    ],
)
def test_failed_refresh_keeps_the_cached_keys(
    google_key: rsa.RSAPrivateKey, failure: httpx2.Response | Exception
) -> None:
    fake = FakeGoogle(google_key)
    fake.signing_keys.get("key-0")
    fake.failure = failure
    fake.now = 2000  # expired
    assert fake.signing_keys.get("key-0") is not None
    assert fake.fetches == 2


def test_only_rs256_rsa_keys_are_used(google_key: rsa.RSAPrivateKey) -> None:
    fake = FakeGoogle(google_key)
    ec_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    fake.extra_jwks = [
        {**ECAlgorithm.to_jwk(ec_key, as_dict=True), "kid": "ec", "alg": "ES256"},
        {"kty": "oct", "k": b64url(b"shared-secret"), "kid": "hmac", "alg": "HS256"},
        {
            **RSAAlgorithm.to_jwk(google_key.public_key(), as_dict=True),
            "kid": "rsa-for-rs512",
            "alg": "RS512",
        },
        {"kty": "RSA", "kid": "broken"},
        "not a key",
    ]
    for kid in ["ec", "hmac", "rsa-for-rs512", "broken"]:
        assert fake.signing_keys.get(kid) is None
    assert fake.signing_keys.get("key-0") is not None


def test_verifier_with_fetched_keys(google_key: rsa.RSAPrivateKey, google: Any) -> None:
    fake = FakeGoogle(google_key)
    verifier = GoogleIdTokenVerifier(google.client_id, fake.signing_keys)
    assert verifier.verify(google.token(headers={"kid": "key-0"})).sub == "100000000000000000001"


def test_tokens_that_cannot_verify_never_cause_fetches(google: Any) -> None:
    fake = FakeGoogle()
    verifier = GoogleIdTokenVerifier(google.client_id, fake.signing_keys)
    for token in ["garbage", forge({"alg": "none", "kid": "x"}, google.claims())]:
        with pytest.raises(InvalidIdTokenError):
            verifier.verify(token)
    assert fake.fetches == 0
