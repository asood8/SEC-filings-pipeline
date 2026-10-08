"""API test fixtures: a seeded database with a search index, a client factory, and Google ID
tokens signed with a test key (tests never call Google)."""

import hashlib
import math
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import jwt
import pytest
import redis
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api.app import RATE_LIMIT_PREFIX, create_app, instrument
from filings_api.embedding_cache import CACHE_PREFIX as EMBEDDING_CACHE_PREFIX
from filings_api.google import GoogleIdTokenVerifier
from filings_api.middleware import CACHE_PREFIX
from filings_common.config import ApiSettings
from filings_common.db import (
    EMBEDDING_DIMENSIONS,
    Company,
    Filing,
    FilingExhibit,
    Form4Filing,
    Form4Transaction,
    Form8KFiling,
    Summary,
    session_factory,
)
from filings_common.embeddings import EmbeddingError
from filings_common.search import rebuild_index

FORM4 = "0001140361-26-000002"
APPLE_8K = "0000320193-26-000001"
MSFT_8K = "0000789019-26-000003"
GOOGLE_CLIENT_ID = "1234-test.apps.googleusercontent.com"
GOOGLE_KID = "test-key-1"
FRONTEND_ORIGIN = "http://localhost:3000"
# Tim Cook's relationship to Apple, as stored for the seeded Form 4
# (form4_filings.reporting_owner_relationships).
COOK_AT_APPLE = {
    "is_director": True,
    "is_officer": True,
    "is_ten_percent_owner": False,
    "is_other": False,
    "officer_title": "Executive Chair",
    "other_text": None,
}


class WordHashEmbedder:
    """Deterministic stand-in for an embedding model: texts sharing words get similar vectors."""

    model = "word-hash"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []  # every query it was asked to embed

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % EMBEDDING_DIMENSIONS] += 1
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingError("down")
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self.fail:
            raise EmbeddingError("down")
        return self._vector(text)


def _summary(accession_no: str, provider: str, headline: str, text: str, **extra: Any) -> Summary:
    return Summary(
        accession_no=accession_no,
        provider=provider,
        model=f"{provider}-model",
        prompt_version=1,
        summary={"headline": headline, "summary": text, "key_points": [text]},
        **extra,
    )


@pytest.fixture
def seeded(clean_db: Engine) -> Engine:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        session.add_all(
            [
                Company(
                    cik=320193,
                    ticker="AAPL",
                    name="Apple Inc.",
                    exchange="Nasdaq",
                    on_watchlist=True,
                ),
                Company(
                    cik=789019,
                    ticker="MSFT",
                    name="MICROSOFT CORP",
                    exchange="Nasdaq",
                    on_watchlist=True,
                ),
                Company(cik=1234567, ticker="ZZZZ", name="Unlisted Example Co", on_watchlist=False),
            ]
        )
        session.flush()
        for accession_no, cik, form, filed in [
            (FORM4, 320193, "4", date(2026, 9, 30)),
            (APPLE_8K, 320193, "8-K", date(2026, 9, 28)),
            (MSFT_8K, 789019, "8-K", date(2026, 9, 29)),
        ]:
            session.add(
                Filing(
                    accession_no=accession_no,
                    cik=cik,
                    form_type=form,
                    filed_at=filed,
                    primary_doc_url=f"https://www.sec.gov/Archives/{accession_no}.htm",
                    raw_object_key=f"filings/{cik}/{accession_no}/doc",
                )
            )
        session.flush()
        session.add_all(
            [
                _summary(
                    FORM4,
                    "template",
                    "Tim Cook sold 50,000 AAPL shares",
                    "Insider sale by the CEO.",
                ),
                _summary(
                    APPLE_8K,
                    "gemini",
                    "Apple names Kevan Parekh chief financial officer",
                    "Apple appointed a new CFO effective October 1.",
                    ungrounded_numbers=[],
                ),
                _summary(
                    MSFT_8K,
                    "gemini",
                    "Microsoft reports record quarterly revenue",
                    "Microsoft reported record revenue and raised guidance.",
                    ungrounded_numbers=["9.9 billion"],
                ),
                Form4Filing(
                    accession_no=FORM4,
                    period_of_report=date(2026, 9, 29),
                    issuer_cik=320193,
                    issuer_name="Apple Inc.",
                    issuer_ticker="AAPL",
                    reporting_owners=["COOK TIMOTHY D"],
                    reporting_owner_ciks=[1214156],
                    # As on his real Apple Form 4s.
                    reporting_owner_relationships=[COOK_AT_APPLE],
                    transaction_codes=["S"],
                    is_10b5_1=True,
                ),
                Form4Transaction(
                    accession_no=FORM4,
                    line=1,
                    is_derivative=False,
                    security_title="Common Stock",
                    transaction_date=date(2026, 9, 29),
                    code="S",
                    shares=30000,
                    price_per_share=254.5,
                    acquired_disposed="D",
                    shares_owned_after=3250000,
                    direct_or_indirect="D",
                ),
                Form4Transaction(
                    accession_no=FORM4,
                    line=2,
                    is_derivative=False,
                    security_title="Common Stock",
                    transaction_date=date(2026, 9, 29),
                    code="S",
                    shares=20000,
                    price_per_share=None,  # given only in a footnote
                    acquired_disposed="D",
                    shares_owned_after=3230000,
                    direct_or_indirect="I",
                ),
                Form8KFiling(accession_no=APPLE_8K, items=["5.02"]),
                Form8KFiling(accession_no=MSFT_8K, items=["2.02", "9.01"]),
                FilingExhibit(
                    accession_no=MSFT_8K,
                    filename="ex991.htm",
                    exhibit_type="EX-99.1",
                    sequence=2,
                    description="PRESS RELEASE",
                    object_key=f"filings/789019/{MSFT_8K}/ex991.htm",
                ),
            ]
        )
    rebuild_index(sessions, WordHashEmbedder())
    return clean_db


# The API's key prefixes in Redis. Tests delete only these: the test database is shared with other
# tests (and other runs, e.g. parallel worktrees), so flushing it would wipe their keys mid-test.
API_KEY_PREFIXES = (RATE_LIMIT_PREFIX, CACHE_PREFIX, EMBEDDING_CACHE_PREFIX)


def clear_api_keys(client: redis.Redis) -> None:
    for prefix in API_KEY_PREFIXES:
        keys = list(client.scan_iter(match=f"{prefix}*", count=500))
        if keys:
            client.delete(*keys)


ClientFactory = Callable[..., TestClient]


@dataclass(frozen=True)
class Ids:
    form4: str = FORM4
    apple_8k: str = APPLE_8K
    msft_8k: str = MSFT_8K


@pytest.fixture
def ids() -> Ids:
    """Accession numbers of the seeded filings (test modules can't import from conftest)."""
    return Ids()


@pytest.fixture
def make_embedder() -> Callable[..., WordHashEmbedder]:
    return WordHashEmbedder


@pytest.fixture(scope="session")
def google_key() -> rsa.RSAPrivateKey:
    """Stands in for Google's signing key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass(frozen=True)
class GoogleTokens:
    """Mints Google-style ID tokens for this app's client ID, signed with the test key."""

    key: rsa.RSAPrivateKey
    client_id: str = GOOGLE_CLIENT_ID
    kid: str = GOOGLE_KID

    def verifier(self) -> GoogleIdTokenVerifier:
        return GoogleIdTokenVerifier(self.client_id, {self.kid: self.key.public_key()})

    def claims(self, **overrides: Any) -> dict[str, Any]:
        """Valid claims; an override of None removes that claim."""
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": self.client_id,
            "sub": "100000000000000000001",
            "email": "ada@example.com",
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
        } | overrides
        return {name: value for name, value in claims.items() if value is not None}

    def token(
        self,
        *,
        key: rsa.RSAPrivateKey | None = None,
        headers: dict[str, Any] | None = None,
        **claims: Any,
    ) -> str:
        return jwt.encode(
            self.claims(**claims),
            key or self.key,
            algorithm="RS256",
            headers={"kid": self.kid} | (headers or {}),
        )


@pytest.fixture
def google(google_key: rsa.RSAPrivateKey) -> GoogleTokens:
    return GoogleTokens(google_key)


@pytest.fixture
def make_client(
    seeded: Engine, redis_client: redis.Redis, google_key: rsa.RSAPrivateKey
) -> Iterator[ClientFactory]:
    """Build a client; by default with generous rate limits, caching off, and sign-in accepting
    tokens from the `google` fixture. It talks https, so Secure cookies round-trip. With
    traced=True, the app is instrumented as when tracing is on."""
    clear_api_keys(redis_client)
    clients: list[TestClient] = []

    def build(**overrides: Any) -> TestClient:
        settings = ApiSettings(
            _env_file=None,
            rate_limit_per_minute=overrides.pop("rate_limit_per_minute", 60_000),
            rate_limit_burst=overrides.pop("rate_limit_burst", 10_000),
            cache_ttl_seconds=overrides.pop("cache_ttl_seconds", 0),
            trust_proxy_headers=overrides.pop("trust_proxy_headers", False),
            cors_origins=overrides.pop("cors_origins", [FRONTEND_ORIGIN]),
            cookie_secure=overrides.pop("cookie_secure", True),
        )
        app = create_app(
            overrides.pop("sessions", None) or session_factory(seeded),
            redis_client=overrides.pop("redis_client", redis_client),
            embedder=overrides.pop("embedder", WordHashEmbedder()),
            settings=settings,
            google_verifier=overrides.pop("google_verifier", GoogleTokens(google_key).verifier()),
        )
        if overrides.pop("traced", False):
            instrument(app)
        client = TestClient(app, base_url="https://testserver")
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()
