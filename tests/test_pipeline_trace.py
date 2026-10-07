"""One trace follows a filing from discovery to its alert, through every stage's real code, and
each stage's metrics count it once.

Discovery, the parser, the summarizer, and the notifier run their real handlers, connected by the
real outbox relay and consume loop. Only the outside world is faked: SEC (canned responses), the
embedding server, and the Discord webhook. Messages go from the relay to the next consume loop in
memory, so the test never touches the pipeline's real topics.
"""

import threading
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from confluent_kafka import Message, TopicPartition
from confluent_kafka.admin import ClusterMetadata
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pymongo.database import Database
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import EdgarSettings
from filings_common.consumer import Handler, run_consumer
from filings_common.db import (
    EMBEDDING_DIMENSIONS,
    Channel,
    Company,
    Subscription,
    User,
    session_factory,
)
from filings_common.edgar import EdgarClient
from filings_common.embeddings import OpenAICompatibleEmbedder
from filings_common.events import Topic
from filings_common.mongo import ParsedFilingStore
from filings_common.outbox import OutboundMessage, relay_batch
from filings_common.storage import LocalObjectStore
from filings_discovery.job import discover
from filings_notifier.channels import Alert
from filings_notifier.handler import NotificationHandler
from filings_parser.forms import PARSERS
from filings_parser.handler import FilingHandler
from filings_summarizer.handler import SummaryHandler
from filings_summarizer.summarizers import Form4TemplateSummarizer

pytestmark = pytest.mark.integration

AMAZON = 1018724
ACCESSION = "0001936006-26-000026"  # a real Form 4: an Amazon officer's sale
FIXTURE = Path(__file__).parent / "fixtures" / "form4" / f"{ACCESSION}.xml"
WEBHOOK = "https://discord.com/api/webhooks/42/amazon-alerts"


class NoLimit:
    def acquire(self) -> float:
        return 0.0


class Discord:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, target: str, alert: Alert) -> None:
        self.sent.append((target, alert.accession_no))


class Relayed:
    """The relay's publisher: keeps the messages for the next stage's consumer."""

    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    def publish(self, messages: Sequence[OutboundMessage]) -> None:
        self.messages.extend(messages)


class InMemorySource:
    """Serves the relayed messages of one topic to the real consume loop, then stops it."""

    def __init__(self, stop: threading.Event, messages: Sequence[OutboundMessage]) -> None:
        self.stop = stop
        self.pending = [
            Message(
                topic=m.topic,
                partition=0,
                offset=offset,
                key=m.key.encode(),
                value=m.value,
                headers=list(m.headers),
            )
            for offset, m in enumerate(messages)
        ]

    def subscribe(self, topics: list[str]) -> None:
        pass

    def poll(self, timeout: float = -1) -> Message | None:
        if not self.pending:
            self.stop.set()
            return None
        return self.pending.pop(0)

    def commit(self, *, message: Message, asynchronous: Literal[False]) -> object:
        return None

    def close(self) -> None:
        pass

    # No partitions, offsets, or topics to measure lag and dead letters by.

    def assignment(self) -> list[TopicPartition]:
        return []

    def position(self, partitions: list[TopicPartition]) -> list[TopicPartition]:
        return partitions

    def committed(
        self, partitions: list[TopicPartition], timeout: float = -1
    ) -> list[TopicPartition]:
        return partitions

    def get_watermark_offsets(
        self, partition: TopicPartition, timeout: float = -1, cached: bool = False
    ) -> tuple[int, int]:
        return (0, 0)

    def list_topics(self, topic: str | None = None, timeout: float = -1) -> Any:
        return ClusterMetadata()


def relay_then_consume(
    sessions: sessionmaker[Session], topic: Topic, handler: Handler, group: str
) -> None:
    """Publish what the previous stage wrote to the outbox, and run `group`'s consumer on it."""
    relayed, dead_letters = Relayed(), Relayed()
    assert relay_batch(sessions, relayed) == 1
    assert [m.topic for m in relayed.messages] == [topic]
    stop = threading.Event()
    source = InMemorySource(stop, relayed.messages)
    run_consumer(source, topic, handler, dead_letters, stop, consumer_group=group)
    assert dead_letters.messages == []


def fake_sec() -> EdgarClient:
    submissions = {
        "cik": str(AMAZON),
        "name": "AMAZON COM INC",
        "filings": {
            "recent": {
                "accessionNumber": [ACCESSION],
                "form": ["4"],
                "filingDate": ["2026-10-02"],
                "acceptanceDateTime": ["2026-10-02T16:05:12.000Z"],
                "primaryDocument": ["xslF345X05/form4.xml"],
            },
            "files": [],
        },
    }
    routes = {
        f"https://data.sec.gov/submissions/CIK{AMAZON:010d}.json": httpx2.Response(
            200, json=submissions
        ),
        f"https://www.sec.gov/Archives/edgar/data/{AMAZON}/000193600626000026/form4.xml": (
            httpx2.Response(200, content=FIXTURE.read_bytes(), headers={"Content-Type": "text/xml"})
        ),
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        return routes.get(str(request.url), httpx2.Response(404))

    settings = EdgarSettings(_env_file=None, contact_email="ops@example.com", max_attempts=1)
    return EdgarClient(settings, NoLimit(), transport=httpx2.MockTransport(handler))


def fake_embedder() -> OpenAICompatibleEmbedder:
    def handler(request: httpx2.Request) -> httpx2.Response:
        vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
        return httpx2.Response(200, json={"data": [{"index": 0, "embedding": vector}]})

    return OpenAICompatibleEmbedder(
        base_url="http://llm/v1",
        model="embed-test",
        document_prefix="",
        query_prefix="",
        dimensions=EMBEDDING_DIMENSIONS,
        transport=httpx2.MockTransport(handler),
    )


def test_one_trace_follows_a_filing_from_discovery_to_its_alert(
    clean_db: Engine,
    mongo_db: Database[dict[str, Any]],
    tmp_path: Path,
    spans: InMemorySpanExporter,
    measured: Any,
) -> None:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        session.add(Company(cik=AMAZON, ticker="AMZN", name="AMAZON COM INC", on_watchlist=True))
        user = User(email="ops@example.com", google_sub="google-1")
        session.add(user)
        session.flush()
        session.add(
            Subscription(
                user_id=user.id, cik=AMAZON, form_types=[], channel=Channel.DISCORD, target=WEBHOOK
            )
        )
    objects, parsed = LocalObjectStore(tmp_path), ParsedFilingStore(mongo_db)
    discord = Discord()

    with fake_sec() as edgar:
        result = discover(
            sessions,
            edgar,
            objects,
            form_types={"4"},
            since=date(2026, 10, 1),
        )
    assert result.new_filings == 1
    relay_then_consume(
        sessions, Topic.FILINGS_DISCOVERED, FilingHandler(sessions, objects, parsed, PARSERS), "p"
    )
    summarize = SummaryHandler(
        sessions, parsed, {"4": Form4TemplateSummarizer()}, embedder=fake_embedder()
    )
    relay_then_consume(sessions, Topic.FILINGS_PARSED, summarize, "s")
    notify = NotificationHandler(sessions, {Channel.DISCORD: discord})
    relay_then_consume(sessions, Topic.FILINGS_READY, notify, "n")
    assert discord.sent == [(WEBHOOK, ACCESSION)]

    finished = spans.get_finished_spans()
    [root] = [s for s in finished if s.name == "discover filing"]
    in_trace = [s for s in finished if s.context.trace_id == root.context.trace_id]
    by_name: dict[str, ReadableSpan] = {}
    for span in in_trace:
        by_name.setdefault(span.name, span)

    def parent(name: str) -> str:
        span = by_name[name]
        assert span.parent is not None, name
        [found] = [s.name for s in in_trace if s.context.span_id == span.parent.span_id]
        return found

    # The filing's trace runs from its discovery to its alert, each hop the child of the last.
    assert root.parent is None
    assert parent("record filing") == "discover filing"
    assert parent("send filings.discovered") == "record filing"
    assert parent("process filings.discovered") == "send filings.discovered"
    assert parent("parse") == "process filings.discovered"
    assert parent("send filings.parsed") == "process filings.discovered"
    assert parent("process filings.parsed") == "send filings.parsed"
    assert parent("summarize") == "process filings.parsed"
    assert parent("embeddings embed-test") == "process filings.parsed"
    assert parent("send filings.ready") == "process filings.parsed"
    assert parent("process filings.ready") == "send filings.ready"
    assert parent("deliver alert") == "process filings.ready"
    # Including the work in between: SEC requests, stores, and MongoDB.
    assert {"GET", "object_store put", "object_store get", "replace_one parsed_filings"} <= set(
        by_name
    )

    # Every stage names the filing, so its trace can be found by accession number.
    for stage in ("process filings.discovered", "process filings.parsed", "process filings.ready"):
        assert (by_name[stage].attributes or {})["filings.accession_no"] == ACCESSION
    assert (by_name["deliver alert"].attributes or {})["filings.outcome"] == "sent"
    for span in finished:
        assert "discord.com" not in span.to_json()
        assert "ops@example.com" not in span.to_json()

    # The metrics count the filing once at each stage, by low-cardinality attributes only.
    form4 = {"filings.form_type": "4"}
    assert measured.total("filings.discovered", form4) == 1
    assert measured.total("filings.parsed", form4) == 1
    assert measured.total("filings.ready", form4) == 1
    sent = {"filings.alert.channel": "discord", "filings.outcome": "sent"}
    assert measured.total("filings.notifications", sent) == 1
    assert measured.total("filings.notifications") == 1
    assert measured.total("filings.dlq.messages") == 0
    assert measured.total("filings.sec.responses", {"filings.status_class": "2xx"}) == 2
    assert measured.count("filings.sec.rate_limit.wait") == 2
    for name in measured.names():
        for attributes in measured.attribute_sets(name):
            assert ACCESSION not in attributes.values()
            assert str(AMAZON) not in map(str, attributes.values())
