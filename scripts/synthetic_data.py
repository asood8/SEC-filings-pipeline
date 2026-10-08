"""Fill a throwaway Postgres database with synthetic, production-shaped data (dev only).

The real watchlist is too young to show what years of filings do to query plans; this builds
years of them, so `scripts/measure_queries.py` can time every regular query at volume
(docs/runbook.md, "Capacity planning").

    uv run python scripts/synthetic_data.py --create \\
        --database-url postgresql+psycopg://filings:filings-local-dev@localhost:5432/synthetic_scale
    uv run python scripts/synthetic_data.py --drop --database-url ...   # when done

It migrates the database to head, then writes (defaults in brackets): companies [500 on the
watchlist among 8,000, like SEC's ticker map], 10 years of their filings [about 450,000: 93%
Form 4, 7% 8-K, about 90 a company a year as on the real watchlist, skewed by company size,
seasonal, on business days], summaries, search documents, embeddings [the newest 50,000], Form 4
key fields and transaction lines, 8-K items and exhibits, users [1,000] with sessions and
subscriptions, the notifications those subscriptions would have received, and outbox rows (30
days of events, a full replay two days ago, and a backlog of unpublished rows). With the defaults
it takes about 9 minutes on the laptop and makes a 2.3 GB database; use smaller numbers to try
it out.

- **Resumable:** each step (the derived rows in chunks of 20,000 filings) commits together with
  a row in `synthetic_progress`, so rerunning the same command after an interruption carries on
  where it stopped, with the first run's settings. An interruption costs one chunk, about 15 s.
- **Deterministic:** the same --seed and --end-date (default today) give the same rows, resumed
  or not: every step has a random generator of its own.
- **Realistic text where search depends on it:** Form 4 summaries come from the real template
  (filings_summarizer.form4_template), search rows from the real build_document, so documents
  have the words and weights real ones have. 8-K summaries are written from a few templates.
- **Embeddings are random** unit vectors around a center per form and per company: the right
  dimension and some of the structure of real ones, but meaningless for relevance. With 50,000
  the HNSW index builds in under a minute with the default 256 MB --maintenance-work-mem (local
  Postgres has a 768 MB memory limit, so don't raise that much).
- **Bulk-loaded:** rows go in with COPY; the secondary indexes (those declared in the models) are
  dropped first and rebuilt from the models afterwards, then every table is vacuumed and analyzed.
- **Safe:** it refuses the database named "filings" and any database with companies that this
  script didn't make. Session tokens are derivable (`synthetic-session-<id>`) so the measuring
  script can sign in.
"""

import argparse
import dataclasses
import hashlib
import json
import math
import random
import sys
import time
from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import accumulate
from typing import Any, Literal

import psycopg
import sqlalchemy as sa
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from filings_common.db import (
    EMBEDDING_DIMENSIONS,
    Base,
    FilingStatus,
    run_migrations,
)
from filings_common.names import company_name
from filings_common.parsed import Form4, Issuer, ReportingOwner, Signature, Transaction
from filings_common.search import build_document
from filings_common.summaries import FilingSummary
from filings_parser.form4 import transaction_rows
from filings_summarizer.form4_template import MODEL as FORM4_MODEL
from filings_summarizer.form4_template import PROVIDER as FORM4_PROVIDER
from filings_summarizer.form4_template import TEMPLATE_VERSION, summarize_form4

EMBEDDING_MODEL = "nomic-embed-text:v1.5"  # the default EMBEDDING_MODEL (Makefile)
SESSION_LIFETIME = timedelta(days=7)  # ApiSettings.session_lifetime_days

# Filing agents that file for many companies (their CIKs start accession numbers).
FORM4_AGENTS = [1209191, 1127602, 1225208, 899243]
FORM8K_AGENTS = [1193125, 1104659, 950170, 1628280]

# Relative filing activity by month: Form 4s peak with the annual grants (February, March) and
# the trading windows after earnings; 8-Ks with earnings releases.
FORM4_MONTHS = [0.8, 1.6, 1.5, 0.8, 1.2, 0.9, 0.7, 1.2, 0.9, 0.7, 1.2, 1.1]
FORM8K_MONTHS = [1.0, 1.4, 0.9, 1.3, 1.4, 0.8, 1.3, 1.2, 0.7, 1.3, 1.1, 0.7]


def _words(text: str) -> list[str]:
    return text.split()


NAME_WORDS = _words(
    "Acme Apex Atlas Beacon Birch Blue Bright Cardinal Cascade Cedar Clear Coastal Columbia "
    "Compass Crescent Crown Delta Eagle Echo Elm Empire Evergreen Falcon First Frontier Garnet "
    "Gateway Global Golden Granite Harbor Harvest Heritage Horizon Iron Keystone Lake Liberty "
    "Lincoln Maple Meridian Midland Monarch National Northern Oak Pacific Peak Pinnacle Pioneer "
    "Prairie Premier Quantum Redwood Ridge River Royal Sierra Silver Summit Sun Titan Trinity "
    "Union United Valley Vanguard Vertex Western Willow"
)
INDUSTRY_WORDS = _words(
    "Holdings Technologies Bancorp Energy Pharmaceuticals Systems Industries Financial "
    "Therapeutics Semiconductor Networks Brands Foods Materials Logistics Health Software "
    "Resources Realty Capital Communications Instruments Biosciences Airlines Motors Retail "
    "Insurance Utilities Chemicals Entertainment"
)
SUFFIXES = ["Inc", "Corp", "Co", "Ltd", "PLC", "Group Inc", "Inc.", "Corporation"]
FIRST_NAMES = _words(
    "James Mary John Patricia Robert Jennifer Michael Linda David Elizabeth William Barbara "
    "Richard Susan Joseph Jessica Thomas Sarah Charles Karen Christopher Lisa Daniel Nancy "
    "Matthew Betty Anthony Sandra Mark Margaret Donald Ashley Steven Kimberly Andrew Emily Paul "
    "Donna Joshua Michelle Kenneth Carol Kevin Amanda Brian Melissa George Deborah Timothy "
    "Stephanie Ronald Rebecca Jason Laura Edward Helen Jeffrey Sharon Ryan Cynthia Jacob "
    "Kathleen Gary Amy Nicholas Angela Eric Shirley Jonathan Anna Stephen Ruth Larry Brenda "
    "Justin Pamela Scott Nicole Brandon Katherine Benjamin Samantha Samuel Christine Gregory"
)
LAST_NAMES = _words(
    "Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez Martinez Hernandez Lopez "
    "Gonzalez Wilson Anderson Thomas Taylor Moore Jackson Martin Lee Perez Thompson White "
    "Harris Sanchez Clark Ramirez Lewis Robinson Walker Young Allen King Wright Scott Torres "
    "Nguyen Hill Flores Green Adams Nelson Baker Hall Rivera Campbell Mitchell Carter Roberts "
    "Gomez Phillips Evans Turner Diaz Parker Cruz Edwards Collins Reyes Stewart Morris Morales "
    "Murphy Cook Rogers Gutierrez Ortiz Morgan Cooper Peterson Bailey Reed Kelly Howard Ramos "
    "Kim Cox Ward Richardson Watson Brooks Chavez Wood James Bennett Gray Mendoza Ruiz Hughes "
    "Price Alvarez Castillo Sanders Patel Myers Long Ross Foster Jimenez Powell Jenkins Perry"
)
OFFICER_TITLES = [
    "Chief Executive Officer",
    "Chief Financial Officer",
    "President",
    "Chief Operating Officer",
    "EVP, General Counsel",
    "SVP & Chief Accounting Officer",
    "EVP & CFO",
    "Chief Technology Officer",
    "EVP, Chief People Officer",
    "SVP, Controller",
    "President and CEO",
    "EVP, Global Operations",
]
CONFERENCES = ["Goldman Sachs", "Morgan Stanley", "J.P. Morgan", "Bank of America", "Barclays"]


@dataclass(frozen=True, slots=True)
class Insider:
    cik: int
    name: str  # as SEC lists people: LAST FIRST MIDDLE
    officer_title: str | None
    is_director: bool


@dataclass(slots=True)
class Company:
    cik: int
    ticker: str
    name: str
    exchange: str | None
    on_watchlist: bool = False
    size: float = 0.0  # standard normal: bigger companies file more and are followed more
    price: float = 50.0
    insiders: list[Insider] = field(default_factory=list)
    insider_weights: list[float] = field(default_factory=list)


@dataclass(slots=True)
class FilingSkeleton:
    accepted_at: datetime
    cik: int
    form_type: str
    accession_no: str = ""
    status: str = FilingStatus.NOTIFIED.value


@dataclass(frozen=True)
class Settings:
    """What the data depends on. Stored in the database by the first run; a resumed run uses the
    stored settings, so every chunk comes from the same ones."""

    companies: int
    other_companies: int
    years: int
    end: date
    seed: int
    embeddings: int
    users: int
    chunk_filings: int = 20_000  # filings per step of derived rows (one transaction each)

    def to_json(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "end": self.end.isoformat()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Settings":
        return cls(**{**data, "end": date.fromisoformat(data["end"])})

    @property
    def start(self) -> date:
        return self.end.replace(year=self.end.year - self.years)

    @property
    def end_at(self) -> datetime:
        return datetime(self.end.year, self.end.month, self.end.day, tzinfo=UTC)

    def rng(self, step: str) -> random.Random:
        """A generator of its own for each step, so a step gives the same rows whether or not
        the run was resumed before it."""
        return random.Random(f"{self.seed}:{step}")


# --- helpers ------------------------------------------------------------------------------------


def libpq_url(url: str) -> str:
    """The SQLAlchemy URL as a plain libpq URL for psycopg."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 50:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def geometric(rng: random.Random, p: float, cap: int) -> int:
    n = 1
    while n < cap and rng.random() > p:
        n += 1
    return n


def business_days(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def unit_vector(rng: random.Random, dims: int) -> list[float]:
    v = [rng.gauss(0.0, 1.0) for _ in range(dims)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def copy_rows(
    conn: psycopg.Connection[Any], table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> int:
    count = 0
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)
            count += 1
    return count


# --- companies ----------------------------------------------------------------------------------


def make_companies(rng: random.Random, settings: Settings) -> list[Company]:
    total = settings.companies + settings.other_companies
    ciks = rng.sample(range(1_000, 2_100_000), total)
    tickers: list[str] = []
    seen: set[str] = set()
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    while len(tickers) < total:
        ticker = "".join(rng.choice(letters) for _ in range(rng.choice([1, 2, 3, 3, 4, 4, 4, 5])))
        if rng.random() < 0.03:
            ticker += rng.choice(["-A", "-B"])
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    companies = []
    for cik, ticker in zip(ciks, tickers, strict=True):
        words = [rng.choice(NAME_WORDS), rng.choice(INDUSTRY_WORDS), rng.choice(SUFFIXES)]
        name = " ".join(words)
        name = name.upper().replace(".", "") if rng.random() < 0.7 else name
        exchange = rng.choices(["Nasdaq", "NYSE", "OTC", "CBOE", None], [45, 40, 10, 2, 3])[0]
        companies.append(Company(cik=cik, ticker=ticker, name=name, exchange=exchange))
    for company in companies[: settings.companies]:
        company.on_watchlist = True
        company.size = rng.gauss(0.0, 1.0)
        company.price = math.exp(rng.gauss(math.log(60), 0.9))
        insiders = 6 + int(4 * math.exp(0.6 * company.size)) + rng.randrange(6)
        for i in range(insiders):
            last, first = rng.choice(LAST_NAMES), rng.choice(FIRST_NAMES)
            middle = rng.choice("ABCDEFGHJKLMNPRSTW")
            officer = i < max(3, insiders // 2)
            company.insiders.append(
                Insider(
                    cik=rng.randrange(1_000_000, 2_100_000),
                    name=f"{last} {first} {middle}",
                    officer_title=OFFICER_TITLES[i % len(OFFICER_TITLES)] if officer else None,
                    is_director=not officer or i == 0,
                )
            )
        # A few insiders (the CEO, the founders) file most of a company's Form 4s.
        company.insider_weights = list(accumulate(1.0 / (i + 1) for i in range(insiders)))
    return companies


# --- filings ------------------------------------------------------------------------------------


def make_filings(
    rng: random.Random, settings: Settings, watchlist: Sequence[Company], start: date
) -> list[FilingSkeleton]:
    days = business_days(start, settings.end - timedelta(days=1))
    cum4 = list(accumulate(FORM4_MONTHS[d.month - 1] for d in days))
    cum8 = list(accumulate(FORM8K_MONTHS[d.month - 1] for d in days))
    filings: list[FilingSkeleton] = []
    for company in watchlist:
        # Most companies have the whole history; some listed during it.
        first = 0 if rng.random() < 0.85 else rng.randrange(len(days))
        years = (len(days) - first) / 261
        rate4 = min(900.0, max(4.0, 60 * math.exp(0.9 * company.size + rng.gauss(0, 0.2))))
        rate8 = min(40.0, max(2.0, 6 * math.exp(0.45 * company.size + rng.gauss(0, 0.2))))
        for form, rate, cum in (("4", rate4, cum4), ("8-K", rate8, cum8)):
            n = poisson(rng, rate * years)
            base = cum[first - 1] if first else 0.0
            picked = rng.choices(days[first:], cum_weights=[w - base for w in cum[first:]], k=n)
            for day in picked:
                # EDGAR accepts filings from 6 a.m. to 10 p.m. Eastern: 10:00-02:00 UTC.
                at = datetime(day.year, day.month, day.day, 10, tzinfo=UTC)
                filings.append(
                    FilingSkeleton(at + timedelta(seconds=rng.randrange(57_600)), company.cik, form)
                )
    filings.sort(key=lambda f: (f.accepted_at, f.cik, f.form_type))
    return filings


def assign_accessions(
    rng: random.Random, filings: Sequence[FilingSkeleton], companies: dict[int, Company], end: date
) -> None:
    sequences: dict[tuple[int, int], int] = {}
    recent = datetime(end.year, end.month, end.day, tzinfo=UTC) - timedelta(days=1)
    for filing in filings:
        company = companies[filing.cik]
        roll = rng.random()
        if filing.form_type == "4":
            if roll < 0.45:
                prefix = filing.cik
            elif roll < 0.8:
                prefix = company.insiders[rng.randrange(len(company.insiders))].cik
            else:
                prefix = rng.choice(FORM4_AGENTS)
        else:
            prefix = filing.cik if roll < 0.55 else rng.choice(FORM8K_AGENTS)
        year = filing.accepted_at.year % 100
        seq = sequences.get((prefix, year), 0) + 1 + (rng.randrange(3) if prefix > 1e6 else 0)
        sequences[(prefix, year)] = seq
        filing.accession_no = f"{prefix:010d}-{year:02d}-{seq:06d}"
        if filing.accepted_at >= recent:  # the newest filings are still moving through
            filing.status = rng.choices(
                ["discovered", "parsed", "ready", "notified", "failed"], [10, 10, 10, 68, 2]
            )[0]
        elif rng.random() < 0.003:
            filing.status = FilingStatus.FAILED.value


def discovered_at(filing: FilingSkeleton) -> datetime:
    """Discovery polls every 10 minutes."""
    minutes = 2 + (int(filing.accession_no[-6:]) % 9)
    return filing.accepted_at + timedelta(minutes=minutes)


def primary_document(filing: FilingSkeleton, company: Company) -> str:
    if filing.form_type == "4":
        return "form4.xml"
    return f"{company.ticker.lower().replace('-', '')}-{filing.accepted_at:%Y%m%d}.htm"


def filing_rows(
    filings: Sequence[FilingSkeleton], companies: dict[int, Company]
) -> Iterator[tuple[Any, ...]]:
    for f in filings:
        document = primary_document(f, companies[f.cik])
        folder = f.accession_no.replace("-", "")
        created = discovered_at(f)
        yield (
            f.accession_no,
            f.cik,
            f.form_type,
            f.accepted_at.date(),
            f.accepted_at,
            f"https://www.sec.gov/Archives/edgar/data/{f.cik}/{folder}/{document}",
            f"filings/{f.cik}/{f.accession_no}/{document}",
            f.status,
            created,
            created + timedelta(minutes=3),
        )


# --- Form 4 content -----------------------------------------------------------------------------

FORM4_SCENARIOS = ["award", "sale", "tax", "exercise", "purchase", "gift", "conversion", "other"]
FORM4_SCENARIO_WEIGHTS = [25, 30, 14, 10, 7, 4, 5, 5]


def _txn(
    *,
    derivative: bool,
    title: str,
    day: date,
    code: str,
    shares: float,
    price: float | None,
    ad: Literal["A", "D"],
    after: float | None,
    ownership: Literal["D", "I"] = "D",
    exercise_price: float | None = None,
) -> Transaction:
    return Transaction.model_construct(
        derivative=derivative,
        security_title=title,
        shares_owned_after=after,
        direct_or_indirect=ownership,
        nature_of_ownership=None if ownership == "D" else "By Trust",
        exercise_price=exercise_price,
        exercise_date=None,
        expiration_date=None,
        underlying_title="Common Stock" if derivative else None,
        underlying_shares=shares if derivative else None,
        footnote_ids=[],
        transaction_date=day,
        code=code,
        equity_swap=False,
        shares=shares,
        price_per_share=price,
        acquired_disposed=ad,
    )


def make_form4(
    rng: random.Random,
    filing: FilingSkeleton,
    company: Company,
    others: Sequence[Company],
    strangers: Sequence[tuple[int, str]],
) -> Form4:
    filed = filing.accepted_at.date()
    period = filed - timedelta(days=rng.choice([0, 1, 1, 2, 2, 2, 3, 4]))
    years_in = (filed - date(2000, 1, 1)).days / 365.25
    price = round(company.price * math.exp(0.08 * (years_in - 20) + rng.gauss(0, 0.05)), 2)
    if rng.random() < 0.92:  # the company's own insiders
        issuer = Issuer(cik=company.cik, name=company.name, trading_symbol=company.ticker)
        count = 1 if rng.random() < 0.93 else rng.choice([2, 2, 3])
        insiders = [
            company.insiders[
                bisect_left(company.insider_weights, rng.random() * company.insider_weights[-1])
            ]
            for _ in range(count)
        ]
        owners = [
            ReportingOwner.model_construct(
                cik=i.cik,
                name=i.name,
                is_director=i.is_director,
                is_officer=i.officer_title is not None,
                is_ten_percent_owner=False,
                is_other=False,
                officer_title=i.officer_title,
                other_text=None,
            )
            for i in dict.fromkeys(insiders)
        ]
        scenario = rng.choices(FORM4_SCENARIOS, FORM4_SCENARIO_WEIGHTS)[0]
    else:  # the company filing as an insider (a 10% owner) of another issuer
        if rng.random() < 0.85 and others:
            other = rng.choice(others)
            issuer = Issuer(cik=other.cik, name=other.name, trading_symbol=other.ticker)
        else:  # an issuer missing from SEC's ticker map
            cik, name = rng.choice(strangers)
            issuer = Issuer(cik=cik, name=name, trading_symbol=None)
        owners = [
            ReportingOwner.model_construct(
                cik=company.cik,
                name=company.name.upper(),
                is_director=False,
                is_officer=False,
                is_ten_percent_owner=True,
                is_other=False,
                officer_title=None,
                other_text=None,
            )
        ]
        scenario = rng.choice(["purchase", "sale"])
    held: float = round(math.exp(rng.gauss(10, 1.5)))
    lines: list[Transaction] = []

    def day() -> date:
        return period + timedelta(days=rng.choice([0, 0, 0, 1, 2]))

    def shares() -> float:
        value = math.exp(rng.gauss(7.5, 1.4))
        return round(value) if rng.random() < 0.8 else round(value, 3)

    def stock(code: str, ad: Literal["A", "D"], n: float, at: float | None) -> None:
        nonlocal held
        held = max(0, held + (n if ad == "A" else -n))
        lines.append(
            _txn(
                derivative=False,
                title="Common Stock",
                day=day(),
                code=code,
                shares=n,
                price=at,
                ad=ad,
                after=held,
            )
        )

    def derivative(code: str, title: str, n: float, strike: float | None) -> None:
        lines.append(
            _txn(
                derivative=True,
                title=title,
                day=day(),
                code=code,
                shares=n,
                price=0.0,
                ad="A" if code == "A" else "D",
                after=round(math.exp(rng.gauss(9, 1.2))),
                exercise_price=strike,
            )
        )

    match scenario:
        case "award":
            if rng.random() < 0.5:
                derivative("A", "Restricted Stock Units", shares(), None)
            stock("A", "A", shares(), 0.0)
        case "sale":
            for _ in range(geometric(rng, 0.45, 30)):
                at = round(price * math.exp(rng.gauss(0, 0.01)), 4)
                stock("S", "D", shares(), None if rng.random() < 0.08 else at)
        case "tax":
            n = shares()
            derivative("M", "Restricted Stock Units", n, None)
            stock("M", "A", n, 0.0)
            stock("F", "D", round(n * 0.4), price)
        case "exercise":
            n, strike = shares(), round(price * rng.uniform(0.3, 0.9), 2)
            derivative("M", "Employee Stock Option (right to buy)", n, strike)
            stock("M", "A", n, strike)
            for _ in range(geometric(rng, 0.5, 10)):
                stock("S", "D", round(n / 2), round(price * math.exp(rng.gauss(0, 0.01)), 4))
        case "purchase":
            for _ in range(geometric(rng, 0.6, 12)):
                stock("P", "A", shares(), round(price * math.exp(rng.gauss(0, 0.01)), 4))
        case "gift":
            stock("G", "D", shares(), None)
        case "conversion":
            n = shares()
            derivative("C", "Class B Common Stock", n, None)
            stock("C", "A", n, 0.0)
        case _:
            stock(rng.choice(["J", "D"]), "D", shares(), price)
    for _ in range(geometric(rng, 0.8, 4) - 1):  # extra lines: more grants and withholdings
        stock(rng.choice(["A", "F"]), "A", shares(), 0.0)
    aff = (
        rng.choices([True, False, None], [60, 30, 10])
        if scenario == "sale"
        else rng.choices([False, None], [70, 30])
    )[0]
    return Form4.model_construct(
        document_type="4",
        period_of_report=period,
        issuer=issuer,
        reporting_owners=owners,
        aff10b5_one=aff,
        transactions=lines,
        holdings=[],
        footnotes={},
        remarks=None,
        signatures=[Signature.model_construct(name=f"/s/ {owners[0].name}", date=filed)],
    )


# --- 8-K content --------------------------------------------------------------------------------

FORM8K_KINDS: list[tuple[str, list[str], float]] = [
    ("earnings", ["2.02", "9.01"], 35),
    ("leadership", ["5.02"], 15),
    ("leadership", ["5.02", "9.01"], 5),
    ("investor", ["7.01", "9.01"], 10),
    ("capital", ["8.01", "9.01"], 8),
    ("capital", ["8.01"], 5),
    ("votes", ["5.07"], 8),
    ("credit", ["1.01", "2.03", "9.01"], 4),
    ("agreement", ["1.01", "9.01"], 4),
    ("acquisition", ["2.01", "9.01"], 2),
    ("bylaws", ["5.03", "9.01"], 2),
    ("restructuring", ["2.05", "7.01"], 1),
    ("cyber", ["1.05"], 0.5),
    ("auditor", ["4.01", "9.01"], 0.5),
]


def _person(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice('ABCDEFGHJKLMNPRSTW')}. {rng.choice(LAST_NAMES)}"


def _billions(rng: random.Random, company: Company) -> str:
    value = math.exp(rng.gauss(0.5 + company.size, 0.3))
    return f"${value:.1f} billion" if value >= 1 else f"${value * 1000:.0f} million"


def make_8k_summary(rng: random.Random, kind: str, company: Company, filed: date) -> FilingSummary:
    name = company_name(company.name)
    when = f"{filed:%B} {filed.day}, {filed.year}"
    match kind:
        case "earnings":
            quarter = {1: 4, 2: 4, 3: 4, 4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2}.get(filed.month, 3)
            fiscal = filed.year - 1 if quarter == 4 else filed.year
            growth = round(rng.gauss(5, 9), 1)
            eps = round(rng.gauss(1.4, 1.1), 2)
            revenue = _billions(rng, company)
            outlook = rng.choice(["raised", "reaffirmed", "lowered"])
            direction = "up" if growth >= 0 else "down"
            headline = rng.choice(
                [
                    f"{name} Reports Fourth Quarter and Fiscal {fiscal} Results"
                    if quarter == 4
                    else f"{name} Reports Q{quarter} {fiscal} Results",
                    f"{name} Q{quarter} Revenue {'Rises' if growth >= 0 else 'Falls'} "
                    f"{abs(growth)}%",
                ]
            )
            summary = (
                f"{name} reported revenue of {revenue} for the {_ordinal(quarter)} quarter of "
                f"fiscal {fiscal}, {direction} {abs(growth)}% from a year earlier, and diluted "
                f"earnings per share of ${eps:.2f}. Operating margin was "
                f"{rng.uniform(5, 35):.1f}%. The company {outlook} its full-year outlook."
            )
            points = [
                f"Revenue of {revenue}, {direction} {abs(growth)}% year over year",
                f"Diluted EPS of ${eps:.2f}",
                f"Full-year guidance {outlook}",
                f"Free cash flow of {_billions(rng, company)}",
            ]
            if rng.random() < 0.5:
                points.append(f"Quarterly dividend of ${rng.uniform(0.1, 1.5):.2f} per share")
        case "leadership":
            person, title = _person(rng), rng.choice(OFFICER_TITLES[:8])
            leaving = _person(rng)
            choice = rng.randrange(3)
            if choice == 0:
                headline = f"{name} Appoints {person} as {title}"
                summary = (
                    f"{name} appointed {person} as {title}, effective {when}, succeeding "
                    f"{leaving}, who will remain as an advisor through the transition. "
                    f"{person} will receive an annual base salary of "
                    f"${rng.randrange(450, 1500)},000 and a sign-on equity award."
                )
            elif choice == 1:
                headline = f"{title} {leaving} to Retire from {name}"
                summary = (
                    f"{leaving}, {title} of {name}, notified the board of the intention to "
                    f"retire. The board has begun a search for a successor; {person} will serve "
                    "in the role on an interim basis."
                )
            else:
                headline = f"{name} Elects {person} to Board of Directors"
                summary = (
                    f"The board of {name} elected {person} as an independent director and a "
                    f"member of the Audit Committee, effective {when}. {person} will receive "
                    "the standard compensation for non-employee directors."
                )
            points = [
                headline,
                f"Effective {when}",
                "No family relationships or related-party transactions were disclosed",
            ]
        case "investor":
            event = rng.choice(CONFERENCES)
            headline = rng.choice(
                [
                    f"{name} to Present at {event} Conference",
                    f"{name} Hosts Investor Day",
                ]
            )
            summary = (
                f"{name} furnished presentation materials for investor meetings, including "
                f"the {event} conference on {when}. The materials discuss the company's "
                "strategy, capital allocation, and long-term financial targets."
            )
            points = [
                "Investor presentation furnished under Regulation FD",
                f"Long-term revenue growth target of {rng.randrange(3, 12)}% a year",
                "Materials are not deemed filed under the Exchange Act",
            ]
        case "capital":
            if rng.random() < 0.5:
                amount = _billions(rng, company)
                headline = f"{name} Announces {amount.title()} Share Repurchase Program"
                summary = (
                    f"The board of {name} authorized the repurchase of up to {amount} of its "
                    "common stock, with no expiration date, in addition to the remaining "
                    "authorization under the existing program."
                )
                points = [f"New buyback authorization of {amount}", "No expiration date"]
            else:
                dividend = rng.uniform(0.1, 1.8)
                headline = f"{name} Declares Quarterly Dividend of ${dividend:.2f} per Share"
                summary = (
                    f"The board of {name} declared a quarterly cash dividend of ${dividend:.2f} "
                    f"per share, payable to shareholders of record as of {when}."
                )
                points = [f"Dividend of ${dividend:.2f} per share", f"Record date {when}"]
        case "votes":
            headline = rng.choice(
                [
                    f"{name} Shareholders Elect Directors and Approve Executive Pay",
                    f"{name} Reports Annual Meeting Voting Results",
                ]
            )
            summary = (
                f"At the annual meeting of {name}, shareholders elected all "
                f"{rng.randrange(7, 14)} director nominees, approved the compensation of named "
                "executive officers on an advisory basis, ratified the independent auditor, and "
                f"{rng.choice(['rejected', 'approved'])} a shareholder proposal on "
                f"{rng.choice(['climate reporting', 'political spending', 'board diversity'])}."
            )
            points = [
                "All director nominees elected",
                f"Say-on-pay approved with {rng.uniform(60, 98):.0f}% support",
                "Auditor ratified",
            ]
        case "credit":
            amount = _billions(rng, company)
            headline = f"{name} Enters {amount.title()} Revolving Credit Agreement"
            summary = (
                f"{name} entered into a five-year revolving credit agreement of {amount} with a "
                "syndicate of banks, replacing its existing facility. Borrowings bear interest "
                f"at SOFR plus a margin of {rng.uniform(0.8, 1.8):.3f}%."
            )
            points = [
                f"{amount} revolving credit facility",
                "Matures in five years",
                "Replaces the existing credit facility",
            ]
        case "agreement":
            partner = f"{rng.choice(NAME_WORDS)} {rng.choice(INDUSTRY_WORDS)}"
            headline = f"{name} Signs Supply Agreement with {partner}"
            summary = (
                f"{name} entered into a multi-year supply agreement with {partner} valued at "
                f"about {_billions(rng, company)}, with options to extend."
            )
            points = [f"Agreement with {partner}", "Multi-year term with extension options"]
        case "acquisition":
            target = f"{rng.choice(NAME_WORDS)} {rng.choice(INDUSTRY_WORDS)}"
            headline = f"{name} Completes Acquisition of {target}"
            summary = (
                f"{name} completed its acquisition of {target} for about "
                f"{_billions(rng, company)} in cash, funded with cash on hand and new debt."
            )
            points = [f"Acquired {target}", "Funded with cash and debt"]
        case "bylaws":
            headline = f"{name} Amends Bylaws"
            summary = (
                f"The board of {name} adopted amended and restated bylaws, effective {when}, "
                "updating advance notice and proxy access provisions."
            )
            points = ["Amended and restated bylaws", "Advance notice provisions updated"]
        case "restructuring":
            headline = f"{name} Announces Restructuring Plan"
            summary = (
                f"{name} approved a restructuring plan to reduce its workforce by about "
                f"{rng.randrange(3, 15)}% and consolidate facilities, with pre-tax charges of "
                f"{_billions(rng, company)} expected through fiscal {filed.year + 1}."
            )
            points = ["Workforce reduction", "Facility consolidation", "Pre-tax charges expected"]
        case "cyber":
            headline = f"{name} Discloses Cybersecurity Incident"
            summary = (
                f"{name} detected unauthorized access to some of its information technology "
                "systems, activated its incident response plan, and notified law enforcement. "
                "The incident has not had a material impact on operations."
            )
            points = ["Unauthorized access detected", "Incident response plan activated"]
        case _:
            headline = f"{name} Changes Independent Registered Public Accounting Firm"
            summary = (
                f"The audit committee of {name} dismissed its independent auditor and engaged a "
                "new firm for the coming fiscal year. There were no disagreements on accounting "
                "matters."
            )
            points = ["Auditor change approved by the audit committee", "No disagreements"]
    return FilingSummary(headline=headline[:200], summary=summary, key_points=points)


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third", 4: "fourth"}[n]


# --- derived rows -------------------------------------------------------------------------------

# Columns of the tables loaded in each chunk, in foreign-key order.
DERIVED_COLUMNS: dict[str, list[str]] = {
    "summaries": [
        "id",
        "accession_no",
        "provider",
        "model",
        "prompt_version",
        "summary",
        "ungrounded_numbers",
        "created_at",
    ],
    "form4_filings": [
        "accession_no",
        "period_of_report",
        "issuer_cik",
        "issuer_name",
        "issuer_ticker",
        "reporting_owners",
        "transaction_codes",
        "is_10b5_1",
        "updated_at",
    ],
    "form4_transactions": [
        "accession_no",
        "line",
        "is_derivative",
        "security_title",
        "transaction_date",
        "code",
        "shares",
        "price_per_share",
        "acquired_disposed",
        "shares_owned_after",
        "direct_or_indirect",
    ],
    "form8k_filings": ["accession_no", "items", "updated_at"],
    "filing_exhibits": [
        "accession_no",
        "filename",
        "exhibit_type",
        "sequence",
        "description",
        "object_key",
    ],
    "filing_search": [
        "accession_no",
        "summary_id",
        "title",
        "company",
        "ticker",
        "body",
        "keywords",
        "updated_at",
    ],
}
SUMMARIZED = frozenset({FilingStatus.READY.value, FilingStatus.NOTIFIED.value})


@dataclass(frozen=True)
class Issuers:
    """Who a company's Form 4s can be about when it files as an insider of another issuer."""

    others: list[Company]
    strangers: list[tuple[int, str]]  # issuers missing from SEC's ticker map


def make_issuers(rng: random.Random, companies: Sequence[Company], watchlist_size: int) -> Issuers:
    strangers = [
        (
            rng.randrange(2_100_000, 2_200_000),
            f"{rng.choice(NAME_WORDS).upper()} {rng.choice(INDUSTRY_WORDS).upper()} INC",
        )
        for _ in range(200)
    ]
    watchlist = companies[:watchlist_size]
    others = list(companies[watchlist_size:])
    return Issuers(others[: len(others) // 2] + list(watchlist), strangers)


def derived_rows(
    rng: random.Random,
    settings: Settings,
    filings: Sequence[FilingSkeleton],
    companies: dict[int, Company],
    issuers: Issuers,
    summary_id: int,
) -> dict[str, list[tuple[Any, ...]]]:
    """Summaries, search documents, and key fields of these filings, by table. Summary ids
    continue from `summary_id`."""
    rows: dict[str, list[tuple[Any, ...]]] = {table: [] for table in DERIVED_COLUMNS}
    kinds = [k for k, _, _ in FORM8K_KINDS]
    items = [i for _, i, _ in FORM8K_KINDS]
    kind_cum = list(accumulate(w for _, _, w in FORM8K_KINDS))
    for filing in filings:
        if filing.status in ("discovered", "failed"):
            continue
        company = companies[filing.cik]
        created = discovered_at(filing)
        summarized = filing.status in SUMMARIZED
        current: tuple[int, FilingSummary] | None = None
        if filing.form_type == "4":
            form = make_form4(rng, filing, company, issuers.others, issuers.strangers)
            rows["form4_filings"].append(
                (
                    filing.accession_no,
                    form.period_of_report,
                    form.issuer.cik,
                    form.issuer.name,
                    form.issuer.trading_symbol,
                    [o.name for o in form.reporting_owners],
                    sorted({t.code for t in form.transactions if t.code}),
                    form.aff10b5_one,
                    created + timedelta(seconds=30),
                )
            )
            rows["form4_transactions"] += [
                tuple(row[column] for column in DERIVED_COLUMNS["form4_transactions"])
                for row in transaction_rows(filing.accession_no, form)
            ]
            if summarized:
                summary = summarize_form4(form)
                summary_id += 1
                rows["summaries"].append(
                    (
                        summary_id,
                        filing.accession_no,
                        FORM4_PROVIDER,
                        FORM4_MODEL,
                        TEMPLATE_VERSION,
                        json.dumps(summary.model_dump(mode="json")),
                        None,
                        created + timedelta(minutes=1),
                    )
                )
                current = (summary_id, summary)
        else:
            choice = bisect_left(kind_cum, rng.random() * kind_cum[-1])
            kind, numbers = kinds[choice], items[choice]
            rows["form8k_filings"].append(
                (filing.accession_no, numbers, created + timedelta(seconds=30))
            )
            if "9.01" in numbers and rng.random() < 0.8:
                stem = company.ticker.lower().replace("-", "")
                for seq, exhibit in enumerate(["EX-99.1", "EX-99.2"][: rng.choice([1, 1, 1, 2])]):
                    filename = (
                        f"{stem}-{exhibit[3:].replace('.', '')}_{filing.accepted_at:%Y%m%d}.htm"
                    )
                    rows["filing_exhibits"].append(
                        (
                            filing.accession_no,
                            filename,
                            exhibit,
                            seq + 2,
                            exhibit,
                            f"filings/{filing.cik}/{filing.accession_no}/{filename}",
                        )
                    )
            if summarized:
                at = created + timedelta(minutes=rng.randrange(2, 9))
                if rng.random() < 0.06:  # summarized again later (a replay with another model)
                    old = make_8k_summary(rng, kind, company, filing.accepted_at.date())
                    summary_id += 1
                    rows["summaries"].append(
                        (
                            summary_id,
                            filing.accession_no,
                            "ollama",
                            "llama3.2:3b",
                            1,
                            json.dumps(old.model_dump(mode="json")),
                            [],
                            at,
                        )
                    )
                    at = min(settings.end_at, at + timedelta(days=rng.randrange(1, 400)))
                summary = make_8k_summary(rng, kind, company, filing.accepted_at.date())
                provider, model = rng.choice(
                    [("gemini", "gemini-3.5-flash-lite"), ("llama.cpp", "qwen2.5-3b-instruct")]
                )
                summary_id += 1
                ungrounded = [] if rng.random() < 0.85 else [f"{rng.uniform(1, 99):.1f}"]
                rows["summaries"].append(
                    (
                        summary_id,
                        filing.accession_no,
                        provider,
                        model,
                        1,
                        json.dumps(summary.model_dump(mode="json")),
                        ungrounded,
                        at,
                    )
                )
                current = (summary_id, summary)
        if current is not None:
            sid, summary = current
            doc = build_document(
                summary,
                company_name=company_name(company.name),
                ticker=company.ticker,
                form_type=filing.form_type,
            )
            rows["filing_search"].append(
                (
                    filing.accession_no,
                    sid,
                    doc.title,
                    doc.company,
                    doc.ticker,
                    doc.body,
                    doc.keywords,
                    created + timedelta(minutes=2),
                )
            )
    return rows


def embedding_rows(
    rng: random.Random,
    settings: Settings,
    filings: Sequence[FilingSkeleton],
    companies: dict[int, Company],
) -> Iterator[tuple[Any, ...]]:
    """The newest searchable filings' embeddings: random unit vectors around a center per form
    and per company (documents from one template, or about one company, are alike)."""
    dims = EMBEDDING_DIMENSIONS
    centers = {"4": unit_vector(rng, dims), "8-K": unit_vector(rng, dims)}
    vectors: dict[int, list[float]] = {}
    count = 0
    for filing in reversed(filings):
        if count >= settings.embeddings:
            return
        if filing.status not in SUMMARIZED:
            continue
        own = vectors.get(filing.cik)
        if own is None:
            own = vectors[filing.cik] = unit_vector(rng, dims)
        form, r = centers[filing.form_type], rng.random
        v = [0.9 * a + 0.5 * b + 0.05 * (r() - 0.5) * 2 for a, b in zip(form, own, strict=True)]
        norm = math.sqrt(sum(x * x for x in v))
        text = "[" + ",".join(f"{x / norm:.5f}" for x in v) + "]"
        count += 1
        yield (
            filing.accession_no,
            EMBEDDING_MODEL,
            text,
            discovered_at(filing) + timedelta(minutes=2),
        )


# --- users --------------------------------------------------------------------------------------


def session_token(session_id: int) -> str:
    """The cookie token of a synthetic session (the table stores only its hash)."""
    return f"synthetic-session-{session_id}"


def make_users(
    rng: random.Random,
    settings: Settings,
    watchlist: Sequence[Company],
    conn: psycopg.Connection[Any],
) -> dict[str, int]:
    end = settings.end_at
    span = timedelta(days=3 * 365)
    users: list[tuple[int, datetime]] = []
    for user_id in range(1, settings.users + 1):
        # Sign-ups grow over three years: more recent users are more numerous.
        users.append((user_id, end - span * (1 - math.sqrt(rng.random()))))
    users.sort(key=lambda u: u[1])
    users = [(i + 1, at) for i, (_, at) in enumerate(users)]
    counts = {
        "users": copy_rows(
            conn,
            "users",
            ["id", "email", "google_sub", "created_at"],
            ((uid, f"user{uid}@example.com", f"synthetic-{uid:08d}", at) for uid, at in users),
        )
    }

    sessions: list[tuple[Any, ...]] = []
    for uid, _ in users:
        live = rng.choices([0, 1, 2, 3, 5, 10], [55, 25, 10, 6, 3, 1])[0]
        for _ in range(live):
            started = end - timedelta(seconds=rng.randrange(int(SESSION_LIFETIME.total_seconds())))
            sessions.append((uid, started, started + SESSION_LIFETIME))
        if rng.random() < 0.05:  # expired since the last sign-in (sign-ins delete these)
            started = end - SESSION_LIFETIME - timedelta(hours=rng.randrange(1, 48))
            sessions.append((uid, started, started + SESSION_LIFETIME))
    sessions.sort(key=lambda s: s[1])
    counts["sessions"] = copy_rows(
        conn,
        "sessions",
        ["id", "user_id", "token_hash", "created_at", "expires_at"],
        (
            (i + 1, uid, hashlib.sha256(session_token(i + 1).encode()).hexdigest(), at, expires)
            for i, (uid, at, expires) in enumerate(sessions)
        ),
    )

    # Popular companies (the big ones) get most subscriptions.
    popularity = list(accumulate(math.exp(1.2 * c.size) for c in watchlist))
    subscriptions: list[tuple[Any, ...]] = []
    for uid, joined in users:
        count = rng.choices([1, 3, 5, 10, 18, 25], [30, 25, 15, 18, 8, 4])[0]
        targets = [
            (
                "discord",
                f"https://discord.com/api/webhooks/{1_000_000 + uid}/"
                f"{hashlib.sha256(f'd{uid}'.encode()).hexdigest()[:40]}",
            )
        ]
        if rng.random() < 0.3:
            targets.append(
                (
                    "slack",
                    f"https://hooks.slack.com/services/T{uid:08d}/B{uid:08d}/"
                    f"{hashlib.sha256(f's{uid}'.encode()).hexdigest()[:24]}",
                )
            )
        if rng.random() < 0.3:
            targets.append(("email", f"user{uid}@example.com"))
        chosen: dict[int, None] = {}
        while len(chosen) < min(count, len(watchlist)):
            pick = bisect_left(popularity, rng.random() * popularity[-1])
            chosen[watchlist[pick].cik] = None
        for cik in chosen:
            channel, target = rng.choice(targets)
            form_types = rng.choices([[], ["8-K"], ["4"]], [60, 25, 15])[0]
            created = joined + (end - joined) * rng.random() ** 2
            subscriptions.append((uid, cik, form_types, channel, target, created))
    subscriptions.sort(key=lambda s: s[5])
    counts["subscriptions"] = copy_rows(
        conn,
        "subscriptions",
        ["id", "user_id", "cik", "form_types", "channel", "target", "created_at"],
        ((i + 1, *s) for i, s in enumerate(subscriptions)),
    )
    return counts


NOTIFICATIONS_SQL = """
INSERT INTO notifications
    (subscription_id, accession_no, status, attempts, last_error, sent_at, created_at, updated_at)
SELECT m.subscription_id, m.accession_no,
       CASE WHEN m.roll < 1 THEN 'failed' ELSE 'sent' END,
       1,
       CASE WHEN m.roll < 1 THEN 'discord webhook returned 404' END,
       CASE WHEN m.roll < 1 THEN NULL ELSE m.discovered + interval '4 minutes' END,
       m.discovered + interval '3 minutes',
       m.discovered + interval '4 minutes'
FROM (
    SELECT s.id AS subscription_id, f.accession_no, f.created_at AS discovered,
           abs(hashtextextended(f.accession_no, s.id)) % 100 AS roll
    FROM subscriptions s
    JOIN filings f ON f.cik = s.cik
    WHERE f.created_at > s.created_at AND f.status = 'notified'
      AND (cardinality(s.form_types) = 0 OR f.form_type = ANY (s.form_types))
    UNION
    SELECT s.id, f.accession_no, f.created_at,
           abs(hashtextextended(f.accession_no, s.id)) % 100
    FROM subscriptions s
    JOIN form4_filings k ON k.issuer_cik = s.cik
    JOIN filings f ON f.accession_no = k.accession_no
    WHERE f.cik <> s.cik AND f.created_at > s.created_at AND f.status = 'notified'
      AND (cardinality(s.form_types) = 0 OR '4' = ANY (s.form_types))
) m
ORDER BY m.discovered, m.subscription_id
"""


# --- outbox -------------------------------------------------------------------------------------


def outbox_rows(
    rng: random.Random, settings: Settings, filings: Sequence[FilingSkeleton]
) -> Iterator[tuple[Any, ...]]:
    """30 days of events, a full `filings-replay discovered` two days ago, and the newest
    events unpublished (a relay backlog). Rows are in id (time) order."""
    end = settings.end_at
    events: list[tuple[datetime, str, FilingSkeleton]] = []
    order = ["discovered", "parsed", "ready", "notified"]
    for filing in filings:
        created = discovered_at(filing)
        if created < end - timedelta(days=30) or filing.status == "failed":
            continue
        reached = order.index(filing.status)
        events.append((created, "filings.discovered", filing))
        if reached >= 1:
            events.append((created + timedelta(seconds=40), "filings.parsed", filing))
        if reached >= 2:
            events.append((created + timedelta(seconds=95), "filings.ready", filing))
    replay = end - timedelta(days=2)
    events += [
        (replay + timedelta(milliseconds=2 * i), "filings.discovered", f)
        for i, f in enumerate(filings)
    ]
    events.sort(key=lambda e: e[0])
    unpublished_from = len(events) - 2_000
    for i, (at, topic, filing) in enumerate(events):
        payload: dict[str, Any] = {
            "accession_no": filing.accession_no,
            "cik": filing.cik,
            "form_type": filing.form_type,
            "schema_version": 1,
        }
        if topic == "filings.discovered":
            payload["filed_at"] = filing.accepted_at.date().isoformat()
            payload["raw_object_key"] = f"filings/{filing.cik}/{filing.accession_no}/doc"
        elif topic == "filings.parsed":
            payload["parser_version"] = 2
        else:
            payload["summary_id"] = rng.randrange(1, 500_000)
        trace = {"traceparent": f"00-{rng.getrandbits(128):032x}-{rng.getrandbits(64):016x}-01"}
        published = None if i >= unpublished_from else at + timedelta(seconds=1)
        yield (
            i + 1,
            topic,
            filing.accession_no,
            json.dumps(payload),
            json.dumps(trace),
            at,
            published,
        )


# --- database -----------------------------------------------------------------------------------

# Records the settings and each finished step, so an interrupted run resumes where it stopped.
PROGRESS_TABLE = "synthetic_progress"
FILING_COLUMNS = [
    "accession_no",
    "cik",
    "form_type",
    "filed_at",
    "accepted_at",
    "primary_doc_url",
    "raw_object_key",
    "status",
    "created_at",
    "updated_at",
]


def secondary_indexes() -> list[sa.Index]:
    """The indexes the models declare (constraints' indexes, which loading needs, aren't)."""
    return [index for table in Base.metadata.sorted_tables for index in table.indexes]


def database_name(url: str) -> str:
    name = make_url(url).database
    if not name or name == "filings":
        sys.exit("refusing: give a throwaway database, not the development database 'filings'")
    return name


def admin_url(url: str) -> str:
    return libpq_url(make_url(url).set(database="postgres").render_as_string(hide_password=False))


def ensure_database(url: str, *, create: bool) -> None:
    name = database_name(url)
    if not create:
        return
    with psycopg.connect(admin_url(url), autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone():
            return
        conn.execute(f'CREATE DATABASE "{name}"')
        print(f"created database {name}")


def drop_database(url: str) -> None:
    name = database_name(url)
    with psycopg.connect(admin_url(url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    print(f"dropped database {name}")


def report_sizes(conn: psycopg.Connection[Any]) -> None:
    rows = conn.execute(
        "SELECT c.relname, c.reltuples::bigint, pg_size_pretty(pg_total_relation_size(c.oid)),"
        " pg_size_pretty(pg_indexes_size(c.oid))"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind = 'r'"
        f" AND c.relname NOT IN ('alembic_version', '{PROGRESS_TABLE}')"
        " ORDER BY pg_total_relation_size(c.oid) DESC"
    ).fetchall()
    print(f"\n{'table':<22} {'rows':>12} {'total':>10} {'indexes':>10}")
    for name, count, total, indexes in rows:
        print(f"{name:<22} {count:>12,} {total:>10} {indexes:>10}")
    size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()
    print(f"database size: {size[0] if size else '?'}")


class Progress:
    """The steps finished so far. A step's rows and its record commit in one transaction, so a
    step is either done or not started."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {PROGRESS_TABLE} (step text PRIMARY KEY,"
            " seconds double precision NOT NULL, detail jsonb, done_at timestamptz DEFAULT now())"
        )
        self.done = {
            row[0] for row in conn.execute(f"SELECT step FROM {PROGRESS_TABLE}").fetchall()
        }

    def settings(self, requested: Settings) -> Settings:
        """The first run's settings: a resumed run must make the rest of the same data."""
        row = self._conn.execute(
            f"SELECT detail FROM {PROGRESS_TABLE} WHERE step = 'settings'"
        ).fetchone()
        if row is None:
            self._record("settings", 0.0, requested.to_json())
            return requested
        stored = Settings.from_json(row[0])
        if stored != requested:
            print(f"resuming with the first run's settings: {stored.to_json()}")
        return stored

    def run(self, name: str, work: Callable[[], str | None], *, transaction: bool = True) -> None:
        """Run a step unless done. `work` returns what to print about it, if anything."""
        if name in self.done:
            return
        print(f"{name} ...", flush=True)
        started = time.monotonic()
        if transaction:
            with self._conn.transaction():
                message = work()
                self._record(name, time.monotonic() - started)
        else:  # e.g. VACUUM, which can't run in a transaction; it must be safe to repeat
            message = work()
            self._record(name, time.monotonic() - started)
        if message:
            print(f"  {message}", flush=True)

    def _record(self, step: str, seconds: float, detail: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            f"INSERT INTO {PROGRESS_TABLE} (step, seconds, detail) VALUES (%s, %s, %s)",
            [step, seconds, Jsonb(detail) if detail is not None else None],
        )
        self.done.add(step)

    def report(self) -> None:
        rows = self._conn.execute(
            f"SELECT step, seconds FROM {PROGRESS_TABLE}"
            " WHERE step NOT IN ('settings', 'done') ORDER BY done_at"
        ).fetchall()
        total = sum(seconds for _, seconds in rows)
        derived = [seconds for step, seconds in rows if step.startswith("derived ")]
        print(f"\nSteps took {total / 60:.1f} min in all, resumed runs included:")
        if derived:
            print(f"  {f'derived rows ({len(derived)} chunks)':<44} {sum(derived):7.1f} s")
        for step, seconds in rows:
            if not step.startswith("derived "):
                print(f"  {step:<44} {seconds:7.1f} s")


def generate(database_url: str, requested: Settings, maintenance_work_mem: str) -> None:
    database_name(database_url)
    print("migrating to head ...", flush=True)
    run_migrations(database_url)
    conn = psycopg.connect(libpq_url(database_url), autocommit=True)
    try:
        exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL", [PROGRESS_TABLE]).fetchone()
        has_companies = conn.execute("SELECT EXISTS (SELECT FROM companies)").fetchone()
        if not (exists and exists[0]) and has_companies and has_companies[0]:
            sys.exit("refusing: the database has companies but wasn't made by this script")
        progress = Progress(conn)
        if "done" in progress.done:
            print("this database is already complete")
            report_sizes(conn)
            return
        settings = progress.settings(requested)
        conn.execute(f"SET maintenance_work_mem = '{maintenance_work_mem}'")
        # Parallel index builds need shared memory beyond Docker's 64 MB /dev/shm.
        conn.execute("SET max_parallel_maintenance_workers = 0")
        _generate(conn, progress, settings, sa.create_engine(database_url).dialect)
        progress.run("done", lambda: None)
        report_sizes(conn)
        progress.report()
    finally:
        conn.close()


def _generate(
    conn: psycopg.Connection[Any], progress: Progress, settings: Settings, dialect: sa.Dialect
) -> None:
    # Everything else follows from the companies and filings, which take seconds to remake.
    rng = settings.rng("skeleton")
    companies = make_companies(rng, settings)
    by_cik = {c.cik: c for c in companies}
    watchlist = companies[: settings.companies]
    filings = make_filings(rng, settings, watchlist, settings.start)
    assign_accessions(rng, filings, by_cik, settings.end)
    chunks = math.ceil(len(filings) / settings.chunk_filings)
    print(f"{len(companies):,} companies, {len(filings):,} filings ({chunks} chunks)")

    indexes = secondary_indexes()
    if "outbox" not in progress.done:  # still loading: bulk loads are faster without them
        for index in indexes:
            conn.execute(f'DROP INDEX IF EXISTS "{index.name}"')

    def load_companies_and_filings() -> str:
        loaded_at = datetime(settings.start.year, settings.start.month, 1, tzinfo=UTC)
        copy_rows(
            conn,
            "companies",
            ["cik", "ticker", "name", "exchange", "on_watchlist", "updated_at"],
            ((c.cik, c.ticker, c.name, c.exchange, c.on_watchlist, loaded_at) for c in companies),
        )
        copy_rows(conn, "filings", FILING_COLUMNS, filing_rows(filings, by_cik))
        return f"companies: {len(companies):,}, filings: {len(filings):,}"

    progress.run("companies and filings", load_companies_and_filings)

    issuers = make_issuers(settings.rng("issuers"), companies, settings.companies)
    for chunk in range(chunks):

        def load_chunk(chunk: int = chunk) -> str:
            row = conn.execute("SELECT coalesce(max(id), 0) FROM summaries").fetchone()
            part = filings[chunk * settings.chunk_filings : (chunk + 1) * settings.chunk_filings]
            rows = derived_rows(
                settings.rng(f"derived {chunk}"),
                settings,
                part,
                by_cik,
                issuers,
                row[0] if row else 0,
            )
            for table, columns in DERIVED_COLUMNS.items():
                copy_rows(conn, table, columns, rows[table])
            return ", ".join(f"{table}: {len(r):,}" for table, r in rows.items() if r)

        progress.run(f"derived {chunk:04d} of {chunks:04d}", load_chunk)

    progress.run(
        "embeddings",
        lambda: "filing_embeddings: {:,}".format(
            copy_rows(
                conn,
                "filing_embeddings",
                ["accession_no", "model", "embedding", "updated_at"],
                embedding_rows(settings.rng("embeddings"), settings, filings, by_cik),
            )
        ),
    )
    progress.run(
        "users, sessions, subscriptions",
        lambda: ", ".join(
            f"{table}: {count:,}"
            for table, count in make_users(settings.rng("users"), settings, watchlist, conn).items()
        ),
    )
    progress.run(
        "notifications",
        lambda: f"notifications: {conn.execute(NOTIFICATIONS_SQL).rowcount:,}",
    )
    progress.run(
        "outbox",
        lambda: "outbox: {:,}".format(
            copy_rows(
                conn,
                "outbox",
                ["id", "topic", "key", "payload", "trace_context", "created_at", "published_at"],
                outbox_rows(settings.rng("outbox"), settings, filings),
            )
        ),
    )

    def set_sequences() -> None:
        for table in ("summaries", "users", "sessions", "subscriptions", "notifications", "outbox"):
            conn.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                f" (SELECT coalesce(max(id), 0) + 1 FROM {table}), false)"
            )

    progress.run("identity sequences", set_sequences)
    for index in indexes:

        def create_index(index: sa.Index = index) -> None:
            create = sa.schema.CreateIndex(index, if_not_exists=True)
            conn.execute(str(create.compile(dialect=dialect)))

        progress.run(f"index {index.name}", create_index)

    def vacuum() -> None:
        for model_table in Base.metadata.sorted_tables:
            conn.execute(f'VACUUM (ANALYZE) "{model_table.name}"')

    progress.run("vacuum and analyze", vacuum, transaction=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="a throwaway database, e.g. postgresql+psycopg://filings:"
        "filings-local-dev@localhost:5432/synthetic_scale",
    )
    parser.add_argument("--create", action="store_true", help="create the database if missing")
    parser.add_argument("--drop", action="store_true", help="drop the database and exit")
    parser.add_argument("--companies", type=int, default=500, help="watchlist companies")
    parser.add_argument(
        "--other-companies",
        type=int,
        default=7_500,
        help="companies off the watchlist (SEC's ticker map has about 8,000)",
    )
    parser.add_argument("--years", type=int, default=10, help="years of filings")
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=datetime.now(UTC).date(),
        help="the data ends the day before",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--embeddings", type=int, default=50_000, help="embed the newest this many filings"
    )
    parser.add_argument("--users", type=int, default=1_000)
    parser.add_argument(
        "--maintenance-work-mem",
        default="256MB",
        help="for the index builds; keep it well under the server's memory limit",
    )
    args = parser.parse_args(argv)
    if args.drop:
        drop_database(args.database_url)
        return 0
    ensure_database(args.database_url, create=args.create)
    generate(
        args.database_url,
        Settings(
            companies=args.companies,
            other_companies=args.other_companies,
            years=args.years,
            end=args.end_date,
            seed=args.seed,
            embeddings=args.embeddings,
            users=args.users,
        ),
        args.maintenance_work_mem,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
