"""The Oracle API client, the daily inventory check, and the off switch, against a fake API."""

import base64
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from filings_common import oci_switch
from filings_common.config import OciInventorySettings
from filings_common.oci import OciClient, OciError
from filings_common.oci_inventory import REQUIRED_STATEMENTS, run, unexpected_resources

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
TENANCY = "ocid1.tenancy.oc1..t"
SETTINGS = OciInventorySettings(
    tenancy_ocid=TENANCY,
    user_ocid="ocid1.user.oc1..u",
    fingerprint="aa:bb",
    region="us-ashburn-1",
    private_key=PEM.decode(),
    min_quota_statements=5,
)


def resource(kind: str, state: str = "AVAILABLE", n: int = 1) -> dict[str, Any]:
    return {"resourceType": kind, "lifecycleState": state, "identifier": f"ocid1.{kind}.{n}"}


EXPECTED_ACCOUNT = [
    resource("Instance", "RUNNING"),
    resource("BootVolume"),
    resource("Volume"),
    resource("Vcn"),
    resource("Subnet"),
    resource("RouteTable", n=1),
    resource("RouteTable", n=2),
    resource("User", "", 1),
    resource("User", "", 2),
    resource("Quota", "ACTIVE"),
    resource("Budget", "ACTIVE"),
]
STATEMENTS = [*REQUIRED_STATEMENTS, "set compute-core quota standard-a1-core-count to 2 in tenancy"]


class FakeOracle:
    """Answers by host and path; records every request."""

    def __init__(self, **changes: Any) -> None:
        self.items: list[dict[str, Any]] = changes.get("items", EXPECTED_ACCOUNT)
        self.regions = changes.get("regions", [{"regionName": "us-ashburn-1"}])
        self.quotas = changes.get(
            "quotas", [{"id": "q1", "name": "filings-always-free", "lifecycleState": "ACTIVE"}]
        )
        self.statements = changes.get("statements", STATEMENTS)
        self.budgets = changes.get(
            "budgets",
            [
                {
                    "displayName": "filings-stay-free",
                    "lifecycleState": "ACTIVE",
                    "amount": 1,
                    "alertRuleCount": 2,
                    "actualSpend": 0,
                }
            ],
        )
        self.instance_state = "TERMINATED"
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/20180409/resources":
            wanted = re.search(r"query (\w+) resources", json.loads(request.content)["query"])
            assert wanted
            kind = wanted[1]
            items = [
                i for i in self.items if kind == "all" or i["resourceType"].lower() == kind.lower()
            ]
            return httpx2.Response(200, json={"items": items})
        if path.endswith("/regionSubscriptions"):
            return httpx2.Response(200, json=self.regions)
        if path == "/20181025/quotas":
            return httpx2.Response(200, json=self.quotas)
        if path.startswith("/20181025/quotas/"):
            return httpx2.Response(200, json={"statements": self.statements})
        if path == "/20190111/budgets":
            return httpx2.Response(200, json=self.budgets)
        if request.method == "GET" and path.startswith("/20160918/instances/"):
            return httpx2.Response(200, json={"lifecycleState": self.instance_state})
        if request.method in ("POST", "DELETE") and path.startswith("/20160918/"):
            return httpx2.Response(204)
        return httpx2.Response(404)

    def done(self, method: str) -> list[str]:
        return [
            f"{r.url.path.split('/')[2]} {r.url.query.decode()}"
            for r in self.requests
            if r.method == method and r.url.host.startswith("iaas.")
        ]


# --- Signing --------------------------------------------------------------------------------------


def test_requests_are_signed_as_oracle_asks() -> None:
    oracle = FakeOracle()
    with OciClient(
        tenancy=TENANCY,
        user="ocid1.user.oc1..u",
        fingerprint="aa:bb",
        private_key_pem=PEM,
        region="us-ashburn-1",
        transport=httpx2.MockTransport(oracle),
        date=lambda: "Sat, 07 Nov 2026 12:00:00 GMT",
    ) as client:
        client.search("query all resources")
        client.get("limits", "/20181025/quotas", compartmentId=TENANCY)
    post, get = oracle.requests
    assert post.url.host == "query.us-ashburn-1.oci.oraclecloud.com"

    def verify(request: httpx2.Request, lines: list[str]) -> None:
        fields = dict(re.findall(r'(\w+)="([^"]*)"', request.headers["authorization"]))
        assert fields["keyId"] == f"{TENANCY}/ocid1.user.oc1..u/aa:bb"
        assert fields["algorithm"] == "rsa-sha256"
        KEY.public_key().verify(  # raises unless the signature covers exactly these lines
            base64.b64decode(fields["signature"]),
            "\n".join(lines).encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    verify(
        get,
        [
            "date: Sat, 07 Nov 2026 12:00:00 GMT",
            f"(request-target): get /20181025/quotas?compartmentId={TENANCY}",
            "host: limits.us-ashburn-1.oci.oraclecloud.com",
        ],
    )
    verify(
        post,
        [
            "date: Sat, 07 Nov 2026 12:00:00 GMT",
            "(request-target): post /20180409/resources?limit=1000",
            "host: query.us-ashburn-1.oci.oraclecloud.com",
            f"content-length: {len(post.content)}",
            "content-type: application/json",
            f"x-content-sha256: {post.headers['x-content-sha256']}",
        ],
    )


def test_search_follows_pages_and_refusals_are_errors() -> None:
    pages = iter(
        [
            httpx2.Response(
                200, json={"items": [resource("Vcn")]}, headers={"opc-next-page": "p2"}
            ),
            httpx2.Response(200, json={"items": [resource("Subnet")]}),
            httpx2.Response(401),
        ]
    )
    seen: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.query.decode())
        return next(pages)

    with OciClient(
        tenancy=TENANCY,
        user="u",
        fingerprint="f",
        private_key_pem=PEM,
        region="r",
        transport=httpx2.MockTransport(answer),
    ) as client:
        assert [i["resourceType"] for i in client.search("query all resources")] == [
            "Vcn",
            "Subnet",
        ]
        assert seen == ["limit=1000", "limit=1000&page=p2"]
        with pytest.raises(OciError, match="HTTP 401"):
            client.search("query all resources")


# --- The inventory --------------------------------------------------------------------------------


def test_only_what_exists_beyond_the_expected_is_counted() -> None:
    counts, over = unexpected_resources(
        [
            *EXPECTED_ACCOUNT,
            resource("Instance", "RUNNING", 2),  # a second VM
            resource("Instance", "TERMINATED", 3),  # gone: Search still lists it
            resource("Volume", n=2),
            resource("AutonomousDatabase"),  # a kind the project doesn't use
            resource("Policy", "ACTIVE", 1),
            resource("Policy", "ACTIVE", 2),  # identity records are free at any number
        ]
    )
    assert over == {"AutonomousDatabase": 1, "Instance": 1, "Volume": 1}
    assert counts["Instance"] == 2


def gauges(measured: Any) -> tuple[Any, Any, Any]:
    return (
        measured.gauge("filings.oci.resources.unexpected"),
        measured.gauge("filings.oci.guardrails.missing"),
        measured.gauge("filings.oci.budget.spend"),
    )


def test_an_account_as_expected_reports_zeros(
    measured: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="filings_common.oci_inventory"):
        assert run(SETTINGS, transport=httpx2.MockTransport(FakeOracle())) == 0
    assert gauges(measured) == ({"": 0}, {"": 0}, {"": 0})
    assert "as expected" in caplog.text
    assert "ocid1." not in caplog.text  # kinds and counts, never an identifier


@pytest.mark.parametrize(
    ("changes", "unexpected", "guardrails", "logged"),
    [
        (
            {"items": [*EXPECTED_ACCOUNT, resource("Instance", "RUNNING", 2)]},
            1,
            0,
            "1 Instance more",
        ),
        ({"quotas": []}, 0, 1, "is missing or inactive"),
        ({"statements": STATEMENTS[1:]}, 0, 1, "was weakened"),
        ({"statements": STATEMENTS[:4]}, 0, 1, "4 statements"),
        ({"budgets": []}, 0, 1, "budget filings-stay-free is missing"),
        (
            {"regions": [{"regionName": "us-ashburn-1"}, {"regionName": "us-phoenix-1"}]},
            0,
            1,
            "subscribed to 2 regions",
        ),
    ],
)
def test_extras_and_weakened_guardrails_are_reported(
    measured: Any,
    caplog: pytest.LogCaptureFixture,
    changes: dict[str, Any],
    unexpected: int,
    guardrails: int,
    logged: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger="filings_common.oci_inventory"):
        assert run(SETTINGS, transport=httpx2.MockTransport(FakeOracle(**changes))) == 0
    found_unexpected, found_guardrails, _ = gauges(measured)
    assert (found_unexpected, found_guardrails) == ({"": unexpected}, {"": guardrails})
    assert logged in caplog.text


def test_spend_and_a_weakened_budget_are_reported(measured: Any) -> None:
    budget = {
        "displayName": "filings-stay-free",
        "lifecycleState": "ACTIVE",
        "amount": 500,
        "alertRuleCount": 1,
        "actualSpend": 3.2,
    }
    assert run(SETTINGS, transport=httpx2.MockTransport(FakeOracle(budgets=[budget]))) == 0
    assert gauges(measured) == ({"": 0}, {"": 1}, {"": 3.2})


def test_an_unreadable_account_fails_the_job(caplog: pytest.LogCaptureFixture) -> None:
    transport = httpx2.MockTransport(lambda request: httpx2.Response(401))
    with caplog.at_level(logging.ERROR, logger="filings_common.oci_inventory"):
        assert run(SETTINGS, transport=transport) == 1
    assert "couldn't read the Oracle account" in caplog.text


# --- The off switch -------------------------------------------------------------------------------


@pytest.fixture
def switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[int, FakeOracle]]:
    key = tmp_path / "key.pem"
    key.write_bytes(PEM)
    config = tmp_path / "config"
    config.write_text(
        f"[DEFAULT]\nuser=ocid1.user.oc1..u\nfingerprint=aa:bb\ntenancy={TENANCY}\n"
        f"region=us-ashburn-1\nkey_file={key}\n"
    )
    real = OciClient.from_config_file.__func__  # type: ignore[attr-defined]
    monkeypatch.setattr(
        OciClient,
        "from_config_file",
        classmethod(lambda cls, **more: real(cls, config, **more)),
    )

    def go(
        action: str, *, answer: str = "", oracle: FakeOracle | None = None
    ) -> tuple[int, FakeOracle]:
        oracle = oracle or FakeOracle()
        monkeypatch.setattr("builtins.input", lambda prompt: answer)
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        return oci_switch.main([action], transport=httpx2.MockTransport(oracle)), oracle

    return go


def test_status_changes_nothing(
    switch: Callable[..., tuple[int, FakeOracle]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, oracle = switch("status")
    assert code == 0
    assert oracle.done("POST") == oracle.done("DELETE") == []
    out = capsys.readouterr().out
    assert "instance: 1 (RUNNING)" in out
    assert "budget filings-stay-free: 0 spent this month" in out


def test_stop_stops_every_vm_and_deletes_nothing(
    switch: Callable[..., tuple[int, FakeOracle]],
) -> None:
    code, oracle = switch("stop")
    assert code == 0
    assert oracle.done("POST") == ["instances action=STOP"]
    assert oracle.done("DELETE") == []


def test_off_needs_the_phrase(switch: Callable[..., tuple[int, FakeOracle]]) -> None:
    code, oracle = switch("off", answer="yes")
    assert code == 1
    assert oracle.done("DELETE") == []


def test_off_terminates_the_vms_then_deletes_the_volumes(
    switch: Callable[..., tuple[int, FakeOracle]],
) -> None:
    code, oracle = switch("off", answer="delete everything")
    assert code == 0
    assert oracle.done("DELETE") == [
        "instances preserveBootVolume=false",
        "volumes ",
        "bootVolumes ",
    ]


def test_off_waits_for_vms_that_are_still_terminating(
    switch: Callable[..., tuple[int, FakeOracle]], capsys: pytest.CaptureFixture[str]
) -> None:
    oracle = FakeOracle()
    oracle.instance_state = "TERMINATING"
    code, oracle = switch("off", answer="delete everything", oracle=oracle)
    assert code == 1  # the volumes wait for a second run
    assert oracle.done("DELETE") == ["instances preserveBootVolume=false"]
    assert "run this again" in capsys.readouterr().out
