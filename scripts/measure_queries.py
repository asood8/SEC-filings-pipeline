"""Time the database's regular queries at volume and flag bad query plans (dev only).

Runs the real code against a database, normally one filled by scripts/synthetic_data.py: the
API's endpoints through FastAPI's test client, and each pipeline stage's database work through
its real handler, with fakes for everything outside Postgres (SEC, Kafka, MongoDB, object
storage, LLMs, webhooks). Each scenario runs --runs times after a warm-up, for p50 and p95. Then
every SQL statement of one more run is explained with EXPLAIN (ANALYZE, BUFFERS) and flagged when
it reads a large table sequentially, sorts on disk, or spends time compiling with JIT. A write
that needs an earlier statement's rows (a search document pointing at the summary just inserted)
can't be explained alone; it is reported with the time the client measured.

    uv run python scripts/measure_queries.py \\
        --database-url postgresql+psycopg://filings:filings-local-dev@localhost:5432/synthetic_scale

- **Leaves the database as it was:** pipeline scenarios run inside a transaction that is rolled
  back, and so does each EXPLAIN ANALYZE (which executes the statement).
- **Production's planner settings:** sessions use production's work_mem and effective_cache_size
  (deploy/k8s/base/postgres.yaml); the local shared_buffers (128 MB) is close to production's
  (192 MB). The laptop's cores are faster than the A1's, so expect higher times there.
- **No Redis:** responses are never cached and nothing is rate limited, so the API numbers are
  the database's plus FastAPI's. Query embeddings are random vectors: semantic results are
  meaningless, their timing is not.
- **Signed-in scenarios** need a synthetic session (`synthetic-session-<id>` tokens); against
  another database they are skipped.
"""

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from filings_api.app import create_app
from filings_api.auth import INSECURE_SESSION_COOKIE, hash_token, start_session
from filings_api.pagination import encode_cursor
from filings_common.config import ApiSettings
from filings_common.consumer import Received
from filings_common.db import EMBEDDING_DIMENSIONS, Channel, session_factory
from filings_common.edgar import EdgarClient
from filings_common.events import FilingDiscovered, FilingParsed, FilingReady, Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.outbox import OutboundMessage, prune_published, relay_batch
from filings_common.storage import ObjectStore
from filings_common.summaries import FilingSummary
from filings_discovery.job import discover
from filings_notifier.channels import Alert
from filings_notifier.handler import NotificationHandler
from filings_parser.forms import PARSERS
from filings_parser.handler import FilingHandler
from filings_summarizer.form8k_prompt import FilingContext
from filings_summarizer.handler import SummaryHandler
from filings_summarizer.summarizers import SummaryResult

REPO = Path(__file__).resolve().parent.parent
FORM4_FIXTURE = REPO / "tests/fixtures/form4/0001197649-26-000012.xml"
# Production's planner-relevant settings (deploy/k8s/base/postgres.yaml).
PRODUCTION_SETTINGS = "-c work_mem=8MB -c effective_cache_size=512MB"
LARGE_TABLE_ROWS = 10_000  # a sequential scan reading more rows than this is flagged
OUTBOX_RETENTION = timedelta(days=7)  # OutboxSettings.retention_days


# --- fakes for everything outside Postgres ------------------------------------------------------


class RandomEmbedder:
    """A deterministic random unit vector per text: right shape, no meaning."""

    def __init__(self, model: str) -> None:
        self.model = model

    def _vector(self, text: str) -> list[float]:
        rng = random.Random(hashlib.sha256(text.encode()).digest())
        v = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMENSIONS)]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class NullSender:
    def send(self, target: str, alert: Alert) -> None:
        pass


class NullPublisher:
    def publish(self, messages: Sequence[OutboundMessage]) -> None:
        pass


class FixtureObjects:
    """Every raw document is the same real Form 4."""

    def __init__(self) -> None:
        self._raw = FORM4_FIXTURE.read_bytes()

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        pass

    def get(self, key: str) -> bytes:
        return self._raw

    def exists(self, key: str) -> bool:
        return True


class FakeParsedStore:
    def __init__(self, parsed: dict[str, Any]) -> None:
        self._parsed = parsed

    def upsert(self, **_: Any) -> None:
        pass

    def get(self, accession_no: str) -> dict[str, Any] | None:
        return {"accession_no": accession_no, "parsed": self._parsed}


class FixedSummarizer:
    def summarize(self, payload: dict[str, Any], context: FilingContext) -> SummaryResult:
        summary = FilingSummary(
            headline=f"{context.company_name} insider sold shares",
            summary=f"An insider of {context.company_name} sold shares in an open-market sale.",
            key_points=["Sold shares", "Open-market sale"],
        )
        return SummaryResult(summary, "template", "form4-template", 1)


class FakeEdgar:
    """SEC's submissions API, answered from the database: every recent filing is known, so
    discovery only runs its queries."""

    def __init__(self, recent: dict[int, list[tuple[str, str, date, datetime]]]) -> None:
        self._recent = recent

    def submissions(self, cik: int) -> dict[str, Any]:
        rows = self._recent.get(cik, [])
        return {
            "filings": {
                "recent": {
                    "accessionNumber": [r[0] for r in rows],
                    "form": [r[1] for r in rows],
                    "filingDate": [r[2].isoformat() for r in rows],
                    "acceptanceDateTime": [r[3].isoformat() for r in rows],
                    "primaryDocument": ["doc.htm" for _ in rows],
                }
            }
        }


# --- statement capture and plans ----------------------------------------------------------------


class Recorder:
    """Records the statements an engine runs while `active`, with each one's time as the client
    saw it."""

    def __init__(self, engine: sa.Engine) -> None:
        self.active = False
        self.statements: list[tuple[str, Any, float]] = []
        sa.event.listen(engine, "before_cursor_execute", self._before)
        sa.event.listen(engine, "after_cursor_execute", self._after)

    def _before(
        self, conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool
    ) -> None:
        conn.info.setdefault("measure_started", []).append(time.perf_counter())

    def _after(
        self, conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool
    ) -> None:
        elapsed_ms = (time.perf_counter() - conn.info["measure_started"].pop()) * 1000
        head = statement.lstrip()[:8].upper()
        # SET LOCAL (semantic search's) is replayed before the statements explained after it.
        wanted = head.startswith(("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "SET LOCA"))
        if self.active and not many and wanted and statement.strip() != "SELECT 1":
            self.statements.append((statement, params, elapsed_ms))

    @contextmanager
    def recording(self) -> Iterator[list[tuple[str, Any, float]]]:
        self.statements = []
        self.active = True
        try:
            yield self.statements
        finally:
            self.active = False


@dataclass
class Plan:
    statement: str
    execution_ms: float
    planning_ms: float
    flags: list[str]
    text: str
    explained: bool = True


def _nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def explain(
    engine: sa.Engine, statement: str, params: Any, client_ms: float, settings: Sequence[str] = ()
) -> Plan:
    """EXPLAIN (ANALYZE, BUFFERS) one statement inside a transaction that is rolled back, after
    the SET LOCAL statements the scenario ran before it.

    A write that needs an earlier statement's rows (a search document pointing at the summary
    inserted just before it) fails on its own: it gets the time the client measured instead."""
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            for setting in settings:
                conn.exec_driver_sql(setting)
            raw = conn.exec_driver_sql(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", params
            ).scalar_one()
        except sa.exc.DBAPIError:
            return Plan(statement, client_ms, 0.0, [], "", explained=False)
        finally:
            tx.rollback()
        tx = conn.begin()
        try:
            for setting in settings:
                conn.exec_driver_sql(setting)
            text = "\n".join(
                row[0]
                for row in conn.exec_driver_sql(f"EXPLAIN (ANALYZE, BUFFERS) {statement}", params)
            )
        finally:
            tx.rollback()
    document = (raw if isinstance(raw, list) else json.loads(raw))[0]
    flags = []
    for node in _nodes(document["Plan"]):
        rows = (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)) * node.get(
            "Actual Loops", 1
        )
        if node["Node Type"] == "Seq Scan" and rows >= LARGE_TABLE_ROWS:
            flags.append(f"seq scan {node['Relation Name']} ({rows:,.0f} rows)")
        if node.get("Sort Space Type") == "Disk":
            flags.append(f"sort on disk ({node.get('Sort Space Used', 0):,} kB)")
    if jit := document.get("JIT"):
        flags.append(f"JIT {jit['Timing']['Total']:.0f} ms")
    return Plan(
        statement=statement,
        execution_ms=document["Execution Time"],
        planning_ms=document["Planning Time"],
        flags=flags,
        text=text,
    )


# --- scenarios ----------------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    run: Callable[[], Any]  # returns how many items an API call returned, or None


@dataclass
class Result:
    name: str
    timings_ms: list[float]
    items: Any
    plans: list[Plan] = field(default_factory=list)

    @property
    def p50(self) -> float:
        return statistics.median(self.timings_ms)

    @property
    def p95(self) -> float:
        ordered = sorted(self.timings_ms)
        return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


@contextmanager
def rolled_back(engine: sa.Engine) -> Iterator[sessionmaker[Session]]:
    """Sessions whose transactions become savepoints of one outer transaction, rolled back."""
    with engine.connect() as conn:
        outer = conn.begin()
        try:
            yield sessionmaker(
                bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
        finally:
            outer.rollback()


def received(topic: str, key: str, value: str) -> Received:
    return Received(topic=topic, partition=0, offset=0, key=key, value=value.encode())


def api_scenarios(engine: sa.Engine, client: TestClient, model: str) -> list[Scenario]:
    q = _Lookup(engine)
    busiest = q.one(
        "SELECT c.ticker, c.cik FROM filings f JOIN companies c USING (cik)"
        " GROUP BY c.cik ORDER BY count(*) DESC, c.cik LIMIT 1"
    )
    median = q.one(
        "SELECT ticker, cik FROM (SELECT c.ticker, c.cik, row_number() OVER (ORDER BY count(*),"
        " c.cik) AS n, count(*) OVER () AS total FROM filings f JOIN companies c USING (cik)"
        " GROUP BY c.cik) t WHERE n = total / 2"
    )
    form4 = q.one(
        "SELECT f.accession_no FROM filings f JOIN form4_filings USING (accession_no)"
        " ORDER BY f.filed_at DESC, f.accession_no DESC LIMIT 1 OFFSET 1000"
    )[0]
    form8k = q.one(
        "SELECT f.accession_no FROM filings f JOIN filing_exhibits USING (accession_no)"
        " ORDER BY f.filed_at DESC, f.accession_no DESC LIMIT 1 OFFSET 100"
    )[0]
    newest = q.one("SELECT max(filed_at) FROM filings")[0]
    deep = encode_cursor(newest.replace(year=newest.year - 5), "9999999999-99-999999")
    page2 = client.get("/filings").json()["next_cursor"]

    def get(path: str, **params: Any) -> Callable[[], Any]:
        def call() -> Any:
            response = client.get(path, params=params)
            response.raise_for_status()
            body = response.json()
            if isinstance(body, dict):
                items = body.get("items", body.get("recent_filings", body.get("owners")))
                return len(items) if isinstance(items, list) else None
            return len(body) if isinstance(body, list) else None

        return call

    big, mid = busiest[0], median[0]
    scenarios = [
        Scenario("feed", get("/filings")),
        Scenario("feed, page 2", get("/filings", cursor=page2)),
        Scenario("feed, 5 years back", get("/filings", cursor=deep)),
        Scenario("feed, 8-Ks", get("/filings", form_type="8-K")),
        Scenario("feed, 8-Ks 5 years back", get("/filings", form_type="8-K", cursor=deep)),
        Scenario(f"feed, busiest company ({big})", get("/filings", cik=busiest[1])),
        Scenario(
            f"feed, median company ({mid}) 8-Ks",
            get("/filings", cik=median[1], form_type="8-K"),
        ),
        Scenario("filing detail, Form 4", get(f"/filings/{form4}")),
        Scenario("filing detail, 8-K", get(f"/filings/{form8k}")),
        Scenario("companies", get("/companies")),
        Scenario("companies, watchlist", get("/companies", watchlist="true")),
        Scenario("companies, q=hold", get("/companies", q="hold")),
        Scenario(f"company ({big})", get(f"/companies/{big}")),
        Scenario(f"insider activity ({big}), 90 days", get(f"/companies/{big}/insider-activity")),
        Scenario(
            f"insider activity ({big}), 365 days",
            get(f"/companies/{big}/insider-activity", days=365),
        ),
        Scenario("insider trades, purchases, 30 days", get("/insider-trades")),
        Scenario("insider trades, sales, 90 days", get("/insider-trades", side="sell", days=90)),
        Scenario("insider trades, sales, 365 days", get("/insider-trades", side="sell", days=365)),
    ]
    searches = [
        ("keyword 'sold'", {"q": "sold", "mode": "keyword"}),
        ("keyword 'shares'", {"q": "shares", "mode": "keyword"}),
        ("keyword 'dividend', 8-Ks", {"q": "dividend", "mode": "keyword", "form_type": "8-K"}),
        (
            "keyword 'credit facility pizza' (relaxed)",
            {"q": "credit facility pizza", "mode": "keyword"},
        ),
        (f"keyword ticker '{big}'", {"q": big, "mode": "keyword"}),
        (f"keyword ticker '{big.lower()}' (pre-query)", {"q": big.lower(), "mode": "keyword"}),
        (
            "keyword 'chief financial officer', company",
            {"q": "chief financial officer", "mode": "keyword", "cik": busiest[1]},
        ),
        ("semantic", {"q": "leadership change", "mode": "semantic"}),
        ("semantic, 8-Ks", {"q": "leadership change", "mode": "semantic", "form_type": "8-K"}),
        (
            f"semantic, company ({mid})",
            {"q": "leadership change", "mode": "semantic", "cik": median[1]},
        ),
        ("hybrid 'share repurchase'", {"q": "share repurchase", "mode": "hybrid"}),
        (
            "hybrid 'leadership change', 8-Ks",
            {"q": "leadership change", "mode": "hybrid", "form_type": "8-K"},
        ),
        (
            f"hybrid 'dividend', company ({mid})",
            {"q": "dividend", "mode": "hybrid", "cik": median[1]},
        ),
    ]
    scenarios += [Scenario(f"search: {name}", get("/search", **p)) for name, p in searches]
    return scenarios


def signed_in_scenarios(engine: sa.Engine, client: TestClient) -> list[Scenario]:
    """/me and /subscriptions as the user with the most subscriptions and a live session."""
    q = _Lookup(engine)
    rows = q.all(
        "SELECT s.id FROM sessions s JOIN (SELECT user_id, count(*) AS n FROM subscriptions"
        " GROUP BY user_id) u USING (user_id) WHERE s.expires_at > now()"
        " ORDER BY u.n DESC, s.id LIMIT 20"
    )
    for (session_id,) in rows:
        token = f"synthetic-session-{session_id}"
        if q.all("SELECT 1 FROM sessions WHERE token_hash = %(h)s", h=hash_token(token)):
            client.cookies.set(INSECURE_SESSION_COOKIE, token)
            break
    else:
        print("no synthetic session found: skipping the signed-in scenarios")
        return []

    def get(path: str) -> Callable[[], Any]:
        def call() -> Any:
            response = client.get(path)
            response.raise_for_status()
            body = response.json()
            return len(body) if isinstance(body, list) else None

        return call

    return [
        Scenario("me", get("/me")),
        Scenario("subscriptions, most-subscribed user", get("/subscriptions")),
    ]


def pipeline_scenarios(engine: sa.Engine, model: str) -> list[Scenario]:
    q = _Lookup(engine)
    newest = q.one("SELECT max(filed_at) FROM filings")[0]
    recent: dict[int, list[tuple[str, str, date, datetime]]] = {}
    for cik, accession_no, form, filed, accepted in q.all(
        "SELECT cik, accession_no, form_type, filed_at, accepted_at FROM (SELECT *, row_number()"
        " OVER (PARTITION BY cik ORDER BY filed_at DESC, accession_no DESC) AS n FROM filings"
        " JOIN companies USING (cik) WHERE on_watchlist) f WHERE n <= 1000"
    ):
        recent.setdefault(cik, []).append((accession_no, form, filed, accepted))
    edgar = cast(EdgarClient, FakeEdgar(recent))
    objects = cast(ObjectStore, FixtureObjects())

    def discovery() -> None:
        with rolled_back(engine) as sessions:
            discover(
                sessions,
                edgar,
                objects,
                form_types={"4", "8-K"},
                since=newest - timedelta(days=30),
                exhibit_form_types={"8-K"},
            )

    # Filings in the status each stage expects, if there are any (else a replay's work).
    discovered = q.one(
        "SELECT accession_no, cik, filed_at, raw_object_key, status FROM filings"
        " WHERE form_type = '4' ORDER BY status = 'discovered' DESC, accession_no LIMIT 1"
    )
    discovered_event = FilingDiscovered(
        accession_no=discovered[0],
        cik=discovered[1],
        form_type="4",
        filed_at=discovered[2],
        raw_object_key=discovered[3],
    )

    def parser() -> None:
        with rolled_back(engine) as sessions:
            handler = FilingHandler(
                sessions, objects, cast(ParsedFilingStore, FakeParsedStore({})), PARSERS
            )
            handler(
                received(
                    Topic.FILINGS_DISCOVERED, discovered[0], discovered_event.model_dump_json()
                )
            )

    parsed = q.one(
        "SELECT accession_no, cik, status FROM filings WHERE form_type = '4'"
        " ORDER BY status = 'parsed' DESC, accession_no LIMIT 1"
    )
    parsed_event = FilingParsed(
        accession_no=parsed[0], cik=parsed[1], form_type="4", parser_version=PARSERS["4"].version
    )

    def summarizer() -> None:
        with rolled_back(engine) as sessions:
            handler = SummaryHandler(
                sessions,
                cast(ParsedFilingStore, FakeParsedStore({})),
                {"4": FixedSummarizer()},
                RandomEmbedder(model),
            )
            handler(received(Topic.FILINGS_PARSED, parsed[0], parsed_event.model_dump_json()))

    # The ready filing of the company with the most subscriptions: the biggest alert fan-out.
    ready = q.one(
        "SELECT f.accession_no, f.cik, f.form_type, s.id, (SELECT count(*) FROM subscriptions"
        " WHERE cik = f.cik) AS n, f.status FROM filings f"
        " JOIN filing_search fs USING (accession_no) JOIN summaries s ON s.id = fs.summary_id"
        " WHERE f.created_at > (SELECT max(created_at) - interval '2 days' FROM filings)"
        " ORDER BY f.status = 'ready' DESC, n DESC, f.accession_no LIMIT 1"
    )
    ready_event = FilingReady(
        accession_no=ready[0], cik=ready[1], form_type=ready[2], summary_id=ready[3]
    )

    def notifier() -> None:
        with rolled_back(engine) as sessions:
            handler = NotificationHandler(sessions, {c: NullSender() for c in Channel})
            handler(received(Topic.FILINGS_READY, ready[0], ready_event.model_dump_json()))

    def relay() -> None:
        with rolled_back(engine) as sessions:
            relay_batch(sessions, NullPublisher(), 100)

    def prune() -> None:
        with rolled_back(engine) as sessions:
            prune_published(sessions, older_than=OUTBOX_RETENTION)

    # The hourly run once caught up: nothing is old enough (the synthetic outbox spans 30 days).
    oldest = q.one("SELECT now() - min(published_at) + interval '1 day' FROM outbox")[0]

    def prune_nothing_due() -> None:
        with rolled_back(engine) as sessions:
            prune_published(sessions, older_than=oldest)

    user = q.one("SELECT id FROM users ORDER BY id DESC LIMIT 1")[0]

    def sign_in() -> None:
        with rolled_back(engine) as sessions, sessions.begin() as session:
            start_session(session, user, timedelta(days=7))

    return [
        Scenario(f"discovery run ({len(recent)} companies, all known)", discovery),
        Scenario(f"parser: Form 4 ({discovered[4]})", parser),
        Scenario(f"summarizer: Form 4 ({parsed[2]}) with embedding", summarizer),
        Scenario(f"notifier: {ready[2]} ({ready[5]}), {ready[4]} subscriptions", notifier),
        Scenario("relay: one batch of 100", relay),
        Scenario("relay: prune one batch", prune),
        Scenario("relay: prune, nothing due", prune_nothing_due),
        Scenario("sign-in: start a session", sign_in),
    ]


class _Lookup:
    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def all(self, sql: str, **params: Any) -> list[Any]:
        with self._engine.connect() as conn:
            return list(conn.exec_driver_sql(sql, params or None).all())

    def one(self, sql: str, **params: Any) -> Any:
        rows = self.all(sql, **params)
        if not rows:
            raise SystemExit(f"the database has no rows for: {sql}")
        return rows[0]


# --- running and reporting ----------------------------------------------------------------------


def measure(scenario: Scenario, runs: int, engine: sa.Engine, recorder: Recorder) -> Result:
    items = scenario.run()  # warm-up
    timings = []
    for _ in range(runs):
        start = time.perf_counter()
        scenario.run()
        timings.append((time.perf_counter() - start) * 1000)
    with recorder.recording() as statements:
        scenario.run()
    result = Result(scenario.name, timings, items)
    settings: list[str] = []
    for sql, params, ms in statements:
        if sql.lstrip().upper().startswith("SET"):
            settings.append(sql)
        else:
            result.plans.append(explain(engine, sql, params, ms, settings))
    return result


def _short(sql: str, width: int = 90) -> str:
    flat = re.sub(r"\s+", " ", sql).strip()
    return flat if len(flat) <= width else flat[: width - 3] + "..."


def report(results: Sequence[Result], plans_dir: Path | None) -> None:
    print("\n| Scenario | Items | p50 ms | p95 ms | Statements | Slowest statement ms | Flags |")
    print("|---|---:|---:|---:|---:|---:|---|")
    for r in results:
        slowest = max((p.execution_ms for p in r.plans), default=0.0)
        flags = sorted({f for p in r.plans for f in p.flags})
        items = "" if r.items is None else r.items
        print(
            f"| {r.name} | {items} | {r.p50:.1f} | {r.p95:.1f} | {len(r.plans)} | "
            f"{slowest:.1f} | {'; '.join(flags)} |"
        )
    print("\nStatements over 5 ms or flagged:")
    for r in results:
        for i, p in enumerate(r.plans, start=1):
            if p.execution_ms > 5 or p.flags:
                notes = [*p.flags, *([] if p.explained else ["client time, not explained"])]
                marks = f"  [{'; '.join(notes)}]" if notes else ""
                print(f"  {r.name} #{i}: {p.execution_ms:.1f} ms{marks}\n    {_short(p.statement)}")
    if plans_dir is not None:
        plans_dir.mkdir(parents=True, exist_ok=True)
        for n, r in enumerate(results, start=1):
            name = re.sub(r"[^a-z0-9]+", "-", r.name.lower()).strip("-")
            body = "\n\n".join(
                f"-- statement {i}: {p.execution_ms:.2f} ms (planning {p.planning_ms:.2f} ms)\n"
                f"{p.statement}\n\n{p.text or '(depends on an earlier statement: not explained)'}"
                for i, p in enumerate(r.plans, start=1)
            )
            (plans_dir / f"{n:02d}-{name}.txt").write_text(body + "\n")
        print(f"\nplans written to {plans_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--runs", type=int, default=20, help="timed runs per scenario")
    parser.add_argument("--only", help="only scenarios whose name contains this text")
    parser.add_argument("--plans-dir", type=Path, help="write every statement's plan here")
    args = parser.parse_args(argv)

    engine = sa.create_engine(
        args.database_url, connect_args={"options": PRODUCTION_SETTINGS}, pool_size=10
    )
    recorder = Recorder(engine)
    with engine.connect() as conn:
        model = conn.exec_driver_sql("SELECT model FROM filing_embeddings LIMIT 1").scalar()
    model = model or "nomic-embed-text:v1.5"
    app = create_app(
        session_factory(engine),
        redis_client=None,
        embedder=RandomEmbedder(model),
        settings=ApiSettings(cookie_secure=False, cache_ttl_seconds=0),
    )
    results = []
    with TestClient(app) as client:
        scenarios = [
            *api_scenarios(engine, client, model),
            *signed_in_scenarios(engine, client),
            *pipeline_scenarios(engine, model),
        ]
        for scenario in scenarios:
            if args.only and args.only.lower() not in scenario.name.lower():
                continue
            print(f"{scenario.name} ...", flush=True)
            results.append(measure(scenario, args.runs, engine, recorder))
    engine.dispose()
    report(results, args.plans_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
