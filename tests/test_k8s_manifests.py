"""Project rules for the Kubernetes manifests, checked on the rendered output.

kubeconform (`make k8s-check`) checks that every object is valid Kubernetes. These tests check
what the project requires on top of that (docs/adr/0017-kubernetes-manifests.md), at both sizes
the VM can have, with and without the small-vm component (docs/adr/0020):
- every container, init containers included, has CPU and memory requests and limits, and their
  total fits that size's budget;
- every pod meets the "restricted" Pod Security Standard that the namespace enforces;
- every image is pinned to a version;
- the config uses real setting names, and every Secret a workload reads has an example;
- no plain Secret is ever deployed from git;
- the image workflow's deploy job renders with the same kustomize as these tests;
- Argo CD deploys the application from the production branch, which only that job writes, and
  the Sealed Secrets controller from main (docs/adr/0019-image-ci-and-deploy.md).

Most render with `kubectl kustomize`, so they are skipped when kubectl is missing (install it
with `make k8s-tools`), unless REQUIRE_K8S_TOOLS=1 (set by `make k8s-check`), which makes them
fail instead. The Argo CD tests only read the YAML and always run.
"""

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_settings import BaseSettings

ROOT = Path(__file__).parents[1]
ARGOCD = ROOT / "deploy" / "argocd"
PRODUCTION = ROOT / "deploy" / "k8s" / "overlays" / "production"
SEALED_SECRETS = ROOT / "deploy" / "k8s" / "platform" / "sealed-secrets"
SECRET_EXAMPLES = ROOT / "deploy" / "k8s" / "secrets-example"
NAMESPACE = "filings"

# The VM budget (brief §7): at most 9 GiB of the 12 GiB node, of which k3s takes about 0.6 GiB
# and Argo CD's limits 0.75 GiB (deploy/argocd/install). The rest is for everything rendered
# here: all long-running pods at their limits, plus the two largest jobs (jobs are short, but
# discovery runs every ten minutes and can overlap a daily one). CPU requests share the node's
# 2 cores with k3s's add-ons (0.2) and Argo CD (0.18).
MEMORY_BUDGET_MIB = (9 * 1024) - 614 - 768
CPU_REQUEST_BUDGET_MILLICORES = 2000 - 200 - 180
# The smaller size, 1 OCPU and 6 GB (the small-vm component), keeps 768 MiB for the OS and Oracle's
# agent instead of 3 GiB: tighter, for a size meant to be temporary.
SMALL_VM_COMPONENT = "../../components/small-vm"
BUDGETS = {
    "full": (MEMORY_BUDGET_MIB, CPU_REQUEST_BUDGET_MILLICORES),
    "small": ((6 * 1024) - 768 - 614 - 768, 1000 - 200 - 180),
}

# Volume types the restricted Pod Security Standard allows.
ALLOWED_VOLUME_TYPES = {
    "configMap",
    "csi",
    "downwardAPI",
    "emptyDir",
    "ephemeral",
    "persistentVolumeClaim",
    "projected",
    "secret",
}
# Config keys that are not settings fields: read by the manifests themselves (DATABASE_URL is
# assembled from POSTGRES_DB; the backup job uploads to BACKUP_S3_BUCKET) or by the
# OpenTelemetry SDK (OTEL_*).
NON_SETTINGS_CONFIG = re.compile(r"POSTGRES_DB|BACKUP_S3_BUCKET|OTEL_[A-Z_]+")


def _kubectl() -> str:
    found = shutil.which("kubectl") or shutil.which("kubectl", path=Path.home() / ".local/bin")
    if found is None:
        message = "kubectl is not installed; run `make k8s-tools`"
        if os.environ.get("REQUIRE_K8S_TOOLS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return found


def _render(directory: Path) -> list[dict[str, Any]]:
    output = subprocess.run(
        [_kubectl(), "kustomize", str(directory)], check=True, capture_output=True, text=True
    ).stdout
    return [doc for doc in yaml.safe_load_all(output) if doc]


def _render_production(workspace: Path, *, small: bool) -> list[dict[str, Any]]:
    """The production overlay at one VM size: rendered from a copy of deploy/k8s whose overlay
    lists the small-vm component or not, whichever the real one does now."""
    copy = workspace / "k8s"
    shutil.copytree(PRODUCTION.parents[1], copy)
    kustomization = copy / "overlays" / "production" / "kustomization.yaml"
    config = yaml.safe_load(kustomization.read_text())
    components = [c for c in config.get("components", []) if c != SMALL_VM_COMPONENT]
    config["components"] = components + ([SMALL_VM_COMPONENT] if small else [])
    kustomization.write_text(yaml.safe_dump(config))
    return _render(copy / "overlays" / "production")


@pytest.fixture(scope="module", params=["full", "small"])
def size(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def production(size: str, tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    return _render_production(tmp_path_factory.mktemp(size), small=size == "small")


@pytest.fixture(scope="module")
def platform() -> list[dict[str, Any]]:
    return _render(SEALED_SECRETS)


@dataclass(frozen=True)
class Pod:
    """A pod template of a workload, and how many copies of it run at once."""

    workload: str  # e.g. "Deployment/api"
    namespace: str
    spec: dict[str, Any]
    replicas: int
    long_running: bool

    def containers(self) -> Iterator[dict[str, Any]]:
        yield from self.spec.get("initContainers", [])
        yield from self.spec["containers"]


def pods(objects: list[dict[str, Any]]) -> list[Pod]:
    found = []
    for obj in objects:
        kind, spec = obj["kind"], obj.get("spec", {})
        name = f"{kind}/{obj['metadata']['name']}"
        namespace = obj["metadata"].get("namespace", "")
        if kind in ("Deployment", "StatefulSet"):
            template, replicas, long_running = spec["template"], spec.get("replicas", 1), True
        elif kind == "Job":
            template, replicas, long_running = spec["template"], 1, False
        elif kind == "CronJob":
            template, replicas, long_running = spec["jobTemplate"]["spec"]["template"], 1, False
        else:
            continue
        found.append(Pod(name, namespace, template["spec"], replicas, long_running))
    return found


def cpu_millicores(quantity: str) -> int:
    return int(quantity[:-1]) if quantity.endswith("m") else int(float(quantity) * 1000)


def memory_mib(quantity: str) -> float:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi)", quantity)
    assert match, f"unexpected memory quantity {quantity!r}"
    return float(match[1]) * units[match[2]]


def pod_total(pod: Pod, kind: str, resource: str) -> float:
    """What the scheduler counts for one pod: init containers run one at a time, before the
    containers, so the pod needs the larger of the biggest init container and the containers'
    sum."""
    to_number = cpu_millicores if resource == "cpu" else memory_mib

    def amount(container: dict[str, Any]) -> float:
        return to_number(container["resources"][kind][resource])

    init = max((amount(c) for c in pod.spec.get("initContainers", [])), default=0)
    return max(init, sum(amount(c) for c in pod.spec["containers"]))


def test_production_renders_one_namespace_with_default_deny(
    production: list[dict[str, Any]],
) -> None:
    namespaced = [o for o in production if o["kind"] != "Namespace"]
    assert {o["metadata"].get("namespace") for o in namespaced} == {NAMESPACE}
    policy = next(o for o in production if o["metadata"]["name"] == "default-deny-ingress")
    assert policy["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}


def test_production_contains_no_plain_secrets(production: list[dict[str, Any]]) -> None:
    # The repository is public: Secrets reach the cluster only as SealedSecrets.
    assert [o["metadata"]["name"] for o in production if o["kind"] == "Secret"] == []


def test_every_container_has_cpu_and_memory_requests_and_limits(
    production: list[dict[str, Any]], platform: list[dict[str, Any]]
) -> None:
    missing = [
        f"{pod.workload}: {container['name']} {kind}.{resource}"
        for pod in pods(production + platform)
        for container in pod.containers()
        for kind in ("requests", "limits")
        for resource in ("cpu", "memory")
        if resource not in container.get("resources", {}).get(kind, {})
    ]
    assert missing == []


def test_requests_never_exceed_limits(
    production: list[dict[str, Any]], platform: list[dict[str, Any]]
) -> None:
    for pod in pods(production + platform):
        for container in pod.containers():
            resources = container["resources"]
            assert cpu_millicores(resources["requests"]["cpu"]) <= cpu_millicores(
                resources["limits"]["cpu"]
            ), pod.workload
            assert memory_mib(resources["requests"]["memory"]) <= memory_mib(
                resources["limits"]["memory"]
            ), pod.workload


def test_memory_limits_and_cpu_requests_fit_the_vm_budget(
    size: str, production: list[dict[str, Any]], platform: list[dict[str, Any]]
) -> None:
    memory_budget, cpu_budget = BUDGETS[size]
    all_pods = pods(production + platform)
    always = [p for p in all_pods if p.long_running]
    jobs = [p for p in all_pods if not p.long_running]

    def two_largest(kind: str, resource: str) -> float:
        return sum(sorted(pod_total(p, kind, resource) for p in jobs)[-2:])

    memory = sum(pod_total(p, "limits", "memory") * p.replicas for p in always)
    memory += two_largest("limits", "memory")
    assert memory <= memory_budget

    cpu = sum(pod_total(p, "requests", "cpu") * p.replicas for p in always)
    cpu += two_largest("requests", "cpu")
    assert cpu <= cpu_budget


def test_the_small_size_runs_no_local_models(size: str, production: list[dict[str, Any]]) -> None:
    # Without them, the services summarize 8-Ks with Gemini alone and search by keywords only.
    names = {(o["kind"], o["metadata"]["name"]) for o in production}
    models = {
        ("Deployment", "llm"),
        ("Deployment", "embeddings"),
        ("PersistentVolumeClaim", "llm-models"),
        ("PersistentVolumeClaim", "embedding-models"),
    }
    config = next(
        o["data"]
        for o in production
        if o["kind"] == "ConfigMap" and o["metadata"]["name"].startswith("filings-config-")
    )
    if size == "small":
        assert names & models == set()
        assert (config["LLM_MODEL"], config["EMBEDDING_MODEL"]) == ("", "")
    else:
        assert models <= names
        assert config["LLM_MODEL"]
        assert config["EMBEDDING_MODEL"]


def test_pods_meet_the_restricted_pod_security_standard(
    production: list[dict[str, Any]], platform: list[dict[str, Any]]
) -> None:
    for pod in pods(production + platform):
        security = pod.spec.get("securityContext", {})
        # An explicit numeric UID: runAsNonRoot alone fails for images whose user is a name.
        assert security.get("runAsNonRoot") is True, pod.workload
        assert security.get("runAsUser", 0) > 0, pod.workload
        assert security.get("seccompProfile") == {"type": "RuntimeDefault"}, pod.workload
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod.spec.get(flag), pod.workload
        for volume in pod.spec.get("volumes", []):
            assert set(volume) - {"name"} <= ALLOWED_VOLUME_TYPES, (pod.workload, volume)
        for container in pod.containers():
            where = f"{pod.workload}: {container['name']}"
            context = container.get("securityContext", {})
            assert context.get("allowPrivilegeEscalation") is False, where
            assert context.get("readOnlyRootFilesystem") is True, where
            assert context.get("capabilities") == {"drop": ["ALL"]}, where
            assert not context.get("privileged"), where


def test_application_pods_get_no_api_token_or_service_variables(
    production: list[dict[str, Any]],
) -> None:
    for pod in pods(production):
        # None of them talks to the Kubernetes API, and Service variables (POSTGRES_PORT=...,
        # LLM_PORT=...) could collide with the settings' environment variables.
        assert pod.spec.get("automountServiceAccountToken") is False, pod.workload
        assert pod.spec.get("enableServiceLinks") is False, pod.workload


def test_long_running_containers_have_probes(production: list[dict[str, Any]]) -> None:
    # Every one can be restarted if it hangs; serving ones also leave the Service while unready.
    for pod in pods(production):
        if not pod.long_running:
            continue
        for container in pod.spec["containers"]:
            assert "livenessProbe" in container, pod.workload
            if container.get("ports"):
                assert "readinessProbe" in container, pod.workload


def _container(objects: list[dict[str, Any]], workload: str) -> dict[str, Any]:
    [pod] = [p for p in pods(objects) if p.workload == workload]
    [container] = pod.containers()
    return container


def test_workers_are_probed_by_their_heartbeat(production: list[dict[str, Any]]) -> None:
    from filings_common import relay
    from filings_notifier import cli as notifier
    from filings_parser import cli as parser
    from filings_summarizer import cli as summarizer

    # Each Deployment checks the heartbeat its process actually writes (filings_common.health).
    workers = {
        "Deployment/outbox-relay": relay.PROCESS_NAME,
        "Deployment/parser": parser.PROCESS_NAME,
        "Deployment/summarizer": summarizer.PROCESS_NAME,
        "Deployment/notifier": notifier.PROCESS_NAME,
    }
    for workload, process in workers.items():
        container = _container(production, workload)
        for probe in ("startupProbe", "livenessProbe"):
            command = container[probe]["exec"]["command"]
            assert command == ["filings-healthcheck", process], (workload, probe)
            # A probe is a fresh Python process: give it more than the default 1 s on a busy node.
            assert container[probe]["timeoutSeconds"] >= 5, (workload, probe)
        # The heartbeat lives in /tmp, which needs a writable volume on a read-only root.
        assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"], workload


def test_the_api_is_ready_only_with_its_database(production: list[dict[str, Any]]) -> None:
    container = _container(production, "Deployment/api")
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    # Longer than /readyz takes to give up on the database (2 s), so the probe sees its 503.
    assert container["readinessProbe"]["timeoutSeconds"] > 2
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"


def test_our_processes_have_time_to_export_their_last_telemetry(
    production: list[dict[str, Any]],
) -> None:
    # The exporters' final flush gives up after 30 s (ADR-0016); the default grace is also 30 s.
    # The migrate Job exports no telemetry, and ignores SIGTERM anyway (ADR-0014).
    exporting = [
        pod
        for pod in pods(production)
        if any("filings-" in c["image"] for c in pod.containers()) and pod.workload != "Job/migrate"
    ]
    assert {"CronJob/discovery", "CronJob/load-companies", "Deployment/api"} <= {
        pod.workload for pod in exporting
    }
    for pod in exporting:
        assert pod.spec.get("terminationGracePeriodSeconds", 30) >= 45, pod.workload


def test_images_are_pinned(
    production: list[dict[str, Any]], platform: list[dict[str, Any]]
) -> None:
    for pod in pods(production + platform):
        for container in pod.containers():
            image = container["image"]
            name, _, tag = image.rpartition(":")
            assert name, image
            assert "/" not in tag, image
            assert tag not in ("", "latest"), image
    ours = {
        c["image"].rpartition(":")[0]
        for pod in pods(production)
        for c in pod.containers()
        if "filings-" in c["image"]
    }
    assert ours == {
        f"ghcr.io/asood8/filings-{service}"
        for service in ("api", "discovery", "parser", "summarizer", "notifier")
    }


def test_the_deploy_job_renders_with_the_same_kustomize() -> None:
    # Before committing new image tags, the deploy job in .github/workflows/images.yml renders
    # production with a standalone kustomize (docs/adr/0019). Versions can render it differently,
    # so it must be the one inside kubectl, which these tests and kubeconform check.
    client = subprocess.run(
        [_kubectl(), "version", "--client", "--output=json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "images.yml").read_text())
    install = next(
        s for s in workflow["jobs"]["deploy"]["steps"] if s.get("name") == "Install kustomize"
    )
    assert json.loads(client)["kustomizeVersion"] == f"v{install['env']['KUSTOMIZE_VERSION']}"


def _argocd_application(name: str) -> dict[str, Any]:
    application: dict[str, Any] = yaml.safe_load((ARGOCD / f"{name}.yaml").read_text())
    assert application["kind"] == "Application"
    assert application["metadata"]["name"] == name
    return application


def test_argo_cd_deploys_the_application_from_the_production_branch() -> None:
    # People edit the manifests on main. The image workflow's deploy job pushes each commit's
    # manifests to production, with its code's image tags, once the images exist and CI passed
    # (tests/test_images_workflow.py). Following main would put a manifest change live before the
    # code it needs, and run main's placeholder tags.
    source = _argocd_application("filings")["spec"]["source"]
    assert source["targetRevision"] == "production"
    assert ROOT / source["path"] == PRODUCTION


def test_argo_cd_deploys_the_sealed_secrets_controller_from_main() -> None:
    # On a new cluster the controller must run before anyone can seal the Secrets the
    # application's first deploy needs, so it can't wait for that deploy. It uses none of our
    # images, so it has nothing to wait for anyway.
    source = _argocd_application("sealed-secrets")["spec"]["source"]
    assert source["targetRevision"] == "main"
    assert ROOT / source["path"] == SEALED_SECRETS


def test_argo_cd_manages_both_applications_from_this_repository() -> None:
    kustomization = yaml.safe_load((ARGOCD / "kustomization.yaml").read_text())
    assert sorted(kustomization["resources"]) == [
        "filings.yaml",
        "project.yaml",
        "sealed-secrets.yaml",
    ]
    project = yaml.safe_load((ARGOCD / "project.yaml").read_text())
    for name in ("filings", "sealed-secrets"):
        spec = _argocd_application(name)["spec"]
        assert spec["project"] == project["metadata"]["name"], name
        assert spec["source"]["repoURL"] in project["spec"]["sourceRepos"], name


def _settings_env_names() -> set[str]:
    from filings_common import config
    from filings_discovery.config import DiscoverySettings
    from filings_notifier.cli import EmailSettings

    classes: list[type[BaseSettings]] = [DiscoverySettings, EmailSettings]
    classes += [
        value
        for value in vars(config).values()
        if isinstance(value, type) and issubclass(value, BaseSettings) and value is not BaseSettings
    ]
    return {
        f"{cls.model_config.get('env_prefix', '')}{field}".upper()
        for cls in classes
        for field in cls.model_fields
    }


def test_config_keys_are_real_setting_names(production: list[dict[str, Any]]) -> None:
    config = next(
        o
        for o in production
        if o["kind"] == "ConfigMap" and o["metadata"]["name"].startswith("filings-config-")
    )
    known = _settings_env_names()
    unknown = [
        key for key in config["data"] if key not in known and not NON_SETTINGS_CONFIG.fullmatch(key)
    ]
    assert unknown == []


def _secret_references(pod: Pod) -> Iterator[tuple[str, str | None]]:
    """(secret name, key) for each Secret the pod reads; key None means all of it."""
    for container in pod.containers():
        for source in container.get("envFrom", []):
            if "secretRef" in source:
                yield source["secretRef"]["name"], None
        for variable in container.get("env", []):
            ref = variable.get("valueFrom", {}).get("secretKeyRef")
            if ref:
                yield ref["name"], ref["key"]


def test_every_secret_read_has_an_example_with_its_keys(production: list[dict[str, Any]]) -> None:
    examples = {}
    for path in SECRET_EXAMPLES.glob("*.yaml"):
        secret = yaml.safe_load(path.read_text())
        assert secret["kind"] == "Secret"
        assert secret["metadata"]["namespace"] == NAMESPACE
        assert path.stem == secret["metadata"]["name"]
        examples[secret["metadata"]["name"]] = set(secret["stringData"])

    referenced: dict[str, set[str]] = {}
    for pod in pods(production):
        for name, key in _secret_references(pod):
            referenced.setdefault(name, set())
            if key is not None:
                referenced[name].add(key)

    assert set(referenced) == set(examples)
    for name, keys in referenced.items():
        assert keys <= examples[name], name
