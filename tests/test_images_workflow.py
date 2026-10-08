"""Rules for the image workflow, .github/workflows/images.yml (docs/adr/0019):

- every job runs only in a public repository, where its runners and GHCR storage are free;
- each architecture builds natively on its own runner, without emulation;
- it builds every service, and production runs exactly those images, all from one commit;
- only the deploy job can write to the repository;
- its own deploy commit can't start it again.
"""

from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "images.yml"
PRODUCTION_IMAGES = ROOT / "deploy" / "k8s" / "overlays" / "production" / "images"
PUBLIC_ONLY = "github.event.repository.private == false"


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return workflow


def _services() -> list[str]:
    return sorted(p.name for p in (ROOT / "services").iterdir() if (p / "pyproject.toml").is_file())


def test_every_job_runs_only_in_a_public_repository() -> None:
    # The gate must come first and be joined only by `&&`, so nothing can bypass it.
    for name, job in _workflow()["jobs"].items():
        condition = " ".join(str(job.get("if", "")).split())
        assert condition == PUBLIC_ONLY or condition.startswith(f"{PUBLIC_ONLY} && "), name
        assert "||" not in condition, name


def test_each_architecture_builds_on_its_own_runner() -> None:
    jobs = _workflow()["jobs"]
    include = jobs["build"]["strategy"]["matrix"]["include"]
    assert {i["arch"]: i["runner"] for i in include} == {
        "amd64": "ubuntu-24.04",
        "arm64": "ubuntu-24.04-arm",
    }
    actions = [step.get("uses", "") for job in jobs.values() for step in job["steps"]]
    assert not [a for a in actions if "qemu" in a], "no emulation"


def test_it_builds_every_service_and_production_runs_them() -> None:
    services = _services()
    jobs = _workflow()["jobs"]
    assert sorted(jobs["build"]["strategy"]["matrix"]["service"]) == services
    assert sorted(jobs["merge"]["strategy"]["matrix"]["service"]) == services

    images = yaml.safe_load((PRODUCTION_IMAGES / "kustomization.yaml").read_text())["images"]
    assert sorted(image["name"] for image in images) == [f"filings-{s}" for s in services]
    for image in images:
        assert image["newName"] == f"ghcr.io/asood8/{image['name']}"
    assert len({str(image["newTag"]) for image in images}) == 1, "all from the same commit"


def test_only_the_deploy_job_can_write_to_the_repository() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        expected = "write" if name == "deploy" else "read"
        assert job["permissions"].get("contents") == expected, name


def test_the_deploy_commit_does_not_start_another_run() -> None:
    workflow = _workflow()
    on = workflow[True]  # PyYAML reads the key `on` as the boolean true (YAML 1.1)
    committed = str((PRODUCTION_IMAGES / "kustomization.yaml").relative_to(ROOT))
    assert not [p for p in on["push"]["paths"] if fnmatch(committed, p)]
    runs = [step.get("run", "") for step in workflow["jobs"]["deploy"]["steps"]]
    assert any("[skip ci]" in run and "git commit" in run for run in runs)
