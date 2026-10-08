"""Rules for the image workflow, .github/workflows/images.yml (docs/adr/0019):

- every job runs only in a public repository, where its runners and GHCR storage are free;
- each architecture builds natively on its own runner, without emulation;
- it builds every service, and production runs exactly those images, all from one commit;
- it runs for every push to main that changes an image input or a manifest, one run at a time;
- only the deploy job can write to the repository, and it pushes only to the production branch,
  which Argo CD deploys, never to main;
- its deploy can't start it, or any other workflow, again.

The second half runs the plan and deploy jobs' shell steps, exactly as the workflow has them,
under bash with the runner's options, against a throwaway git repository (an "origin" and a
developer's clone, in a temporary directory deleted after each test), with stubs for gh, docker,
and kustomize: a first deploy, a manifest-only push, a code push, an older run re-run, a rollback
started by hand, a failed CI run, and a production branch moved by hand.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
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


def _image_inputs() -> list[str]:
    value = str(_workflow()["env"]["IMAGE_INPUTS"])
    assert "\n" not in value.strip(), "the plan job reads IMAGE_INPUTS as one line"
    return value.split()


def _run_scripts() -> Iterator[tuple[str, dict[str, Any]]]:
    for name, job in _workflow()["jobs"].items():
        for step in job["steps"]:
            if "run" in step:
                yield name, step


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


def test_it_runs_for_pushes_to_main_that_change_an_image_input_or_a_manifest() -> None:
    on = _workflow()[True]  # PyYAML reads the key `on` as the boolean true (YAML 1.1)
    assert on["push"]["branches"] == ["main"]
    expected = [f"{p}/**" if (ROOT / p).is_dir() else p for p in _image_inputs()]
    assert on["push"]["paths"] == [*expected, "deploy/**"]
    assert set(on["workflow_dispatch"]["inputs"]) == {"commit", "rollback"}


def test_the_image_inputs_cover_everything_the_build_reads() -> None:
    # The deploy reuses the images of the newest commit that changed an image input, which is only
    # right if nothing else goes into an image: .dockerignore's allowlist and the Dockerfile.
    inputs = _image_inputs()
    allowed = [
        line[1:]
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.startswith("!")
    ]
    read = [*allowed, "docker/service.Dockerfile", ".dockerignore"]
    assert allowed
    for path in read:
        top = path.split("/*")[0]
        assert any(top == i or top.startswith(f"{i}/") for i in inputs), path


def test_build_and_merge_run_only_when_the_images_are_missing() -> None:
    jobs = _workflow()["jobs"]
    assert set(jobs["plan"]["outputs"]) == {"source", "image_commit", "tag", "build"}
    for name in ("build", "merge"):
        assert "needs.plan.outputs.build == 'true'" in jobs[name]["if"], name
    # The deploy also runs when they were skipped, but never after a failure.
    deploy = jobs["deploy"]
    assert deploy["needs"] == ["plan", "merge"]
    assert "!cancelled() && !failure()" in " ".join(deploy["if"].split())
    assert "github.ref == 'refs/heads/main'" in deploy["if"]


def test_only_the_deploy_job_can_write_to_the_repository() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        expected = "write" if name == "deploy" else "read"
        assert job["permissions"].get("contents") == expected, name


def test_the_deploy_job_pushes_only_to_the_production_branch() -> None:
    pushes = [
        " ".join(push.split())
        for _, step in _run_scripts()
        for push in re.findall(r"git push[^;\n]*", step["run"].replace("\\\n", " "))
    ]
    assert pushes == [
        'git push --quiet --force-with-lease="refs/heads/production:$PRODUCTION_HEAD"'
        " origin HEAD:refs/heads/production"
    ]
    for job, step in _run_scripts():
        assert set(re.findall(r"refs/heads/[\w.-]+", step["run"])) <= {"refs/heads/production"}, (
            job,
            step["name"],
        )
    # It builds the deploy commit on the source commit, not on main's newest commit, whose images
    # may not exist yet.
    [checkout] = [
        s for s in _workflow()["jobs"]["deploy"]["steps"] if "checkout" in s.get("uses", "")
    ]
    assert checkout["with"]["ref"] == "${{ needs.plan.outputs.source }}"
    # And production is what Argo CD deploys (tests/test_k8s_manifests.py checks the rest).
    application = yaml.safe_load((ROOT / "deploy" / "argocd" / "filings.yaml").read_text())
    assert application["spec"]["source"]["targetRevision"] == "production"


def test_no_workflow_runs_for_pushes_to_the_production_branch() -> None:
    # Production holds only deploy commits; CI already ran on their source commits on main.
    for path in sorted(WORKFLOW.parent.glob("*.y*ml")):
        on = yaml.safe_load(path.read_text())[True]
        events = on if isinstance(on, dict) else dict.fromkeys([on] if isinstance(on, str) else on)
        if "push" in events:
            branches = (events["push"] or {}).get("branches", [])
            assert branches, f"{path.name} runs for pushes to every branch"
            assert not [b for b in branches if fnmatch("production", b)], path.name


def test_runs_take_turns_in_push_order() -> None:
    concurrency = _workflow()["concurrency"]
    assert concurrency == {"group": "images-${{ github.ref }}", "cancel-in-progress": False}


def test_the_deploy_does_not_start_another_run() -> None:
    # It pushes only to production (above), while the workflow runs only for main, and its
    # message skips push-triggered workflows. (GITHUB_TOKEN's pushes start none anyway.)
    workflow = _workflow()
    assert workflow[True]["push"]["branches"] == ["main"]
    runs = [step.get("run", "") for step in workflow["jobs"]["deploy"]["steps"]]
    assert any("[skip ci]" in run and "git commit" in run for run in runs)


def test_scripts_read_expressions_only_from_the_environment() -> None:
    # `${{ }}` inside a script is pasted in as code (script injection), and can't run outside
    # Actions, as the tests below run them.
    for job, step in _run_scripts():
        assert "${{" not in step["run"], (job, step["name"])


# --- The plan and deploy jobs' scripts, run against a throwaway repository ----------------------

REPOSITORY = "asood8/SEC-filings-pipeline"
RUN_URL = f"https://github.com/{REPOSITORY}/actions/runs/123"
IMAGES = "deploy/k8s/overlays/production/images/kustomization.yaml"
CONFIG = "deploy/k8s/overlays/production/config.env"
CODE = "services/api/src/filings_api/app.py"
BOT = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"  # git's form
# Steps that aren't run: the kustomize download (a stub is on PATH instead).
NOT_RUN = {"Install kustomize"}

# gh: `run list` prints CI's run on the commit (or nothing), `run watch` exits with its result.
STUB_GH = """#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "run list") [[ -z $STUB_CI_RUN ]] || echo "$STUB_CI_RUN" ;;
  "run watch") exit "$STUB_CI_EXIT" ;;
  *) exit 2 ;;
esac
"""
# docker: `buildx imagetools inspect` finds the images listed in the registry file.
STUB_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
[[ "$1 $2 $3" == "buildx imagetools inspect" ]] || exit 2
if grep -qxF -- "$4" "$STUB_REGISTRY"; then
  echo "Name: $4"
else
  echo "ERROR: $4: not found" >&2
  exit 1
fi
"""
# The wait for CI sleeps between attempts.
STUB_SLEEP = """#!/usr/bin/env bash
echo "sleep $*" >> "$STUB_LOG"
"""
# kustomize: `edit set image NAME=*:TAG...` sets those images' newTag in ./kustomization.yaml,
# which is in kustomize's own layout, so only those lines change (as with kustomize 5.8.1,
# docs/adr/0019); `build DIR` checks that DIR is a kustomization.
STUB_KUSTOMIZE = """#!{python}
import os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("kustomize " + " ".join(args) + "\\n")
if args[:3] == ["edit", "set", "image"]:
    tags = {{}}
    for arg in args[3:]:
        name, _, image = arg.partition("=")
        assert image.startswith("*:"), arg
        tags[name] = image[2:]
    path = pathlib.Path("kustomization.yaml")
    lines, current, changed = path.read_text().splitlines(keepends=True), None, set()
    for i, line in enumerate(lines):
        if line.startswith("- name: "):
            current = line.split(": ", 1)[1].strip()
        elif line.startswith("  newTag: ") and current in tags:
            lines[i] = "  newTag: " + tags[current] + "\\n"
            changed.add(current)
    if changed != set(tags):
        sys.exit("unknown images: " + " ".join(sorted(set(tags) - changed)))
    path.write_text("".join(lines))
elif args[:1] == ["build"] and len(args) == 2:
    if not pathlib.Path(args[1], "kustomization.yaml").is_file():
        sys.exit("not a kustomization: " + args[1])
else:
    sys.exit("unexpected arguments")
"""


def _git_env() -> dict[str, str]:
    # Nothing from the developer's git configuration or a surrounding git hook.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str) -> str:
    people = {
        "GIT_AUTHOR_NAME": "Developer",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Developer",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env() | people,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _resolve(value: Any, context: Mapping[str, str]) -> str:
    """Replaces each `${{ expression }}` with its value; an unknown expression fails the test."""

    def lookup(match: re.Match[str]) -> str:
        expression = match[1]
        assert expression in context, f"no value for ${{{{ {expression} }}}} in the test"
        return context[expression]

    return re.sub(r"\$\{\{\s*(.+?)\s*\}\}", lookup, str(value))


def _condition_holds(condition: str, context: Mapping[str, str]) -> bool:
    match = re.fullmatch(r"(steps\.\w+\.outputs\.\w+) == '([^']*)'", condition.strip())
    assert match, f"the test can't evaluate the condition {condition!r}"
    return context.get(match[1], "") == match[2]


@dataclass
class Run:
    """One simulated run of the workflow: the plan job's outputs, and how far it got."""

    plan: dict[str, str] = field(default_factory=dict)
    built: bool = False
    failed: str | None = None  # the step that failed, if one did
    log: str = ""


@dataclass
class Repo:
    """A bare "origin" standing in for GitHub, and a developer's clone that pushes to main."""

    root: Path

    @property
    def origin(self) -> Path:
        return self.root / "origin.git"

    @property
    def dev(self) -> Path:
        return self.root / "dev"

    @property
    def registry(self) -> Path:
        return self.root / "registry.txt"

    @property
    def stub_log(self) -> Path:
        return self.root / "stubs.log"

    @classmethod
    def create(cls, root: Path) -> "Repo":
        repo = cls(root)
        _git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(repo.origin))
        _git(root, "clone", "--quiet", str(repo.origin), str(repo.dev))
        _git(repo.dev, "symbolic-ref", "HEAD", "refs/heads/main")
        production = ROOT / "deploy" / "k8s" / "overlays" / "production"
        for path in ("kustomization.yaml", "config.env", "images/kustomization.yaml"):
            target = repo.dev / "deploy" / "k8s" / "overlays" / "production" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(production / path, target)
        (repo.dev / CODE).parent.mkdir(parents=True)
        (repo.dev / CODE).write_text("VERSION = 1\n")
        (repo.dev / "README.md").write_text("Pipeline\n")
        _git(repo.dev, "add", ".")
        _git(repo.dev, "commit", "--quiet", "-m", "Start")
        _git(repo.dev, "push", "--quiet", "origin", "main")

        stubs = root / "stubs"
        stubs.mkdir()
        for name, text in {
            "gh": STUB_GH,
            "docker": STUB_DOCKER,
            "sleep": STUB_SLEEP,
            "kustomize": STUB_KUSTOMIZE.format(python=sys.executable),
        }.items():
            (stubs / name).write_text(text)
            (stubs / name).chmod(0o755)
        repo.registry.touch()
        repo.stub_log.touch()
        return repo

    def push(self, path: str, text: str, message: str) -> str:
        """Commits a change to main and pushes it; returns the commit."""
        (self.dev / path).parent.mkdir(parents=True, exist_ok=True)
        (self.dev / path).write_text(text)
        _git(self.dev, "add", path)
        _git(self.dev, "commit", "--quiet", "-m", message)
        _git(self.dev, "push", "--quiet", "origin", "main")
        return _git(self.dev, "rev-parse", "HEAD")

    def head(self, branch: str) -> str | None:
        found = _git(self.origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")
        return found or None

    def show(self, revision: str, path: str) -> str:
        return _git(self.origin, "show", f"{revision}:{path}")

    def tags(self, revision: str) -> set[str]:
        images = yaml.safe_load(self.show(revision, IMAGES))["images"]
        return {str(image["newTag"]) for image in images}

    def run(
        self,
        *,
        sha: str | None = None,
        dispatch: bool = False,
        commit: str = "",
        rollback: bool = False,
        ci_ran: bool = True,
        ci_passed: bool = True,
        before_step: Mapping[str, Callable[[], None]] | None = None,
    ) -> Run:
        """Runs the workflow for a push of `sha` (default: main), or by hand with `dispatch`.

        Build and merge are simulated: the images appear in the registry. `before_step` runs
        callbacks just before the named steps, to change things while the job runs.
        """
        sha = sha or self.head("main")
        assert sha
        context = {
            "inputs.commit": commit if dispatch else "",
            "inputs.rollback": str(rollback).lower() if dispatch else "",
            "github.token": "stub-token",
            "github.server_url": "https://github.com",
            "github.repository": REPOSITORY,
            "github.run_id": "123",
        }
        runner = self.root / "runner"
        runner.mkdir(exist_ok=True)
        env = _git_env() | {
            "PATH": f"{self.root / 'stubs'}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(runner),
            "RUNNER_TEMP": str(runner),
            "GITHUB_SHA": sha,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_NAME": "main",
            "GITHUB_EVENT_NAME": "workflow_dispatch" if dispatch else "push",
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_PATH": str(runner / "path"),
            "GITHUB_STEP_SUMMARY": str(runner / "summary"),
            "STUB_LOG": str(self.stub_log),
            "STUB_REGISTRY": str(self.registry),
            "STUB_CI_RUN": "42" if ci_ran else "",
            "STUB_CI_EXIT": "0" if ci_passed else "1",
        }
        result = Run()
        if not self._job("plan", env, context, result, before_step or {}):
            return result
        definition = _workflow()["jobs"]["plan"]
        result.plan = {k: _resolve(v, context) for k, v in definition["outputs"].items()}
        context |= {f"needs.plan.outputs.{k}": v for k, v in result.plan.items()}

        if result.plan["build"] == "true":  # build and merge publish every image under the tag
            images = re.findall(
                r"^  newName: (\S+)$", self.show(result.plan["source"], IMAGES), re.M
            )
            with self.registry.open("a") as registry:
                registry.writelines(f"{image}:{result.plan['tag']}\n" for image in images)
            result.built = True

        self._job("deploy", env, context, result, before_step or {})
        return result

    def _job(
        self,
        job: str,
        env: dict[str, str],
        context: dict[str, str],
        result: Run,
        before_step: Mapping[str, Callable[[], None]],
    ) -> bool:
        """Runs a job's steps in order like a runner, until one fails."""
        workflow = _workflow()
        definition = workflow["jobs"][job]
        work = self.root / f"work-{job}"  # the job's workspace, empty until the checkout
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        output = self.root / "output"
        for step in definition["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("actions/checkout@"):
                ref = _resolve(step.get("with", {}).get("ref", env["GITHUB_SHA"]), context)
                _git(self.root, "clone", "--quiet", "--no-checkout", str(self.origin), str(work))
                _git(work, "checkout", "--quiet", "--detach", ref)
                continue
            if uses or step["name"] in NOT_RUN:
                continue  # registry login: the docker stub needs none
            if "if" in step and not _condition_holds(step["if"], context):
                continue
            if step["name"] in before_step:
                before_step[step["name"]]()
            script = self.root / "step.sh"
            script.write_text(step["run"])
            output.write_text("")
            step_env = env | {"GITHUB_OUTPUT": str(output)}
            for scope in (workflow, definition, step):
                step_env |= {k: _resolve(v, context) for k, v in scope.get("env", {}).items()}
            # How a runner runs a `run:` step on Linux.
            process = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                cwd=work,
                env=step_env,
                capture_output=True,
                text=True,
            )
            result.log += f"--- {job}: {step['name']}\n{process.stdout}{process.stderr}"
            for line in output.read_text().splitlines():
                key, _, value = line.partition("=")
                context[f"steps.{step.get('id')}.outputs.{key}"] = value
            if process.returncode != 0:
                result.failed = step["name"]
                return False
        return True


@pytest.fixture
def repo() -> Iterator[Repo]:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    with tempfile.TemporaryDirectory(prefix="images-workflow-") as scratch:
        yield Repo.create(Path(scratch))


def _assert_deploys(repo: Repo, source: str, image_commit: str) -> str:
    """Production is a deploy commit of `source` with `image_commit`'s images; returns it."""
    production = repo.head("production")
    assert production
    assert _git(repo.origin, "rev-parse", f"{production}^") == source, "on top of the source"
    assert repo.tags(production) == {f"sha-{image_commit[:7]}"}
    # The source commit's manifests, with only the image tags changed.
    assert _git(repo.origin, "diff", "--name-only", source, production) == IMAGES
    message = _git(repo.origin, "log", "-1", "--format=%B", production)
    assert message == (
        f"Deploy {source[:7]} (images sha-{image_commit[:7]}) [skip ci]\n\n"
        f"Source commit {source}\nImage commit {image_commit}\nRun {RUN_URL}"
    )
    assert _git(repo.origin, "log", "-1", "--format=%(trailers)", production) == ""
    assert (
        _git(repo.origin, "log", "-1", "--format=%an <%ae>|%cn <%ce>", production) == f"{BOT}|{BOT}"
    )
    return production


def test_the_first_deploy_creates_the_production_branch(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    run = repo.run()
    assert run.failed is None, run.log
    assert run.plan == {"source": a, "image_commit": a, "tag": f"sha-{a[:7]}", "build": "true"}
    assert "There is no production branch yet; this first deploy creates it." in run.log
    _assert_deploys(repo, a, a)
    assert repo.head("main") == a, "the deploy never writes to main"
    assert f"--commit {a} " in repo.stub_log.read_text(), "it waited for CI on the source commit"


def test_a_manifest_only_push_reuses_the_images(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    assert repo.run().failed is None
    b = repo.push(CONFIG, "LOG_LEVEL=DEBUG\n", "Change a setting")
    run = repo.run()
    assert run.failed is None, run.log
    assert run.plan == {"source": b, "image_commit": a, "tag": f"sha-{a[:7]}", "build": "false"}
    assert not run.built
    production = _assert_deploys(repo, b, a)
    assert repo.show(production, CONFIG) == "LOG_LEVEL=DEBUG"
    assert repo.head("main") == b


def test_a_code_push_builds_new_images(repo: Repo) -> None:
    repo.push(CODE, "VERSION = 2\n", "Change the API")
    assert repo.run().failed is None
    repo.push(CONFIG, "LOG_LEVEL=DEBUG\n", "Change a setting")
    assert repo.run().failed is None
    c = repo.push(CODE, "VERSION = 3\n", "Change the API again")
    run = repo.run()
    assert run.failed is None, run.log
    assert run.plan == {"source": c, "image_commit": c, "tag": f"sha-{c[:7]}", "build": "true"}
    _assert_deploys(repo, c, c)


def test_a_run_deploys_its_own_commit_when_main_has_moved_on(repo: Repo) -> None:
    # The newer commit's images aren't built yet, so its manifests must not go out with these.
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    b = repo.push(CODE, "VERSION = 3\n", "Change the API again")
    run = repo.run(sha=a)
    assert run.failed is None, run.log
    _assert_deploys(repo, a, a)
    assert repo.head("main") == b


def test_images_a_run_never_built_are_built_by_the_next_one(repo: Repo) -> None:
    # The code push's run was replaced while it waited (only the newest waiting run is kept).
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    b = repo.push(CONFIG, "LOG_LEVEL=DEBUG\n", "Change a setting")
    run = repo.run()
    assert run.failed is None, run.log
    assert run.plan == {"source": b, "image_commit": a, "tag": f"sha-{a[:7]}", "build": "true"}
    _assert_deploys(repo, b, a)


def test_a_rerun_of_an_older_run_leaves_production_alone(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    assert repo.run().failed is None
    b = repo.push(CONFIG, "LOG_LEVEL=DEBUG\n", "Change a setting")
    assert repo.run().failed is None
    deployed = repo.head("production")

    for sha in (a, b):  # an older commit, and the deployed one
        run = repo.run(sha=sha)
        assert run.failed is None, run.log
        assert f"Production already deploys {b}, which is {sha} or a later commit" in run.log
        assert repo.head("production") == deployed
    # Nothing was built for them either.
    assert run.plan["build"] == "false"


def test_a_rollback_started_by_hand_deploys_an_older_commit(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    assert repo.run().failed is None
    b = repo.push(CODE, "VERSION = 3\n", "Break the API")
    assert repo.run().failed is None

    # Without "rollback", an older commit is left alone.
    run = repo.run(dispatch=True, commit=a[:7])
    assert run.failed is None, run.log
    assert "Production already deploys" in run.log
    _assert_deploys(repo, b, b)

    run = repo.run(dispatch=True, commit=a[:7], rollback=True)
    assert run.failed is None, run.log
    assert run.plan == {"source": a, "image_commit": a, "tag": f"sha-{a[:7]}", "build": "false"}
    assert f"Production deploys {b}, which isn't an earlier commit of {a}; replacing it." in run.log
    _assert_deploys(repo, a, a)
    assert repo.head("main") == b

    # The next push deploys normally again, with main's newest code: b's, unless main reverts it
    # (docs/runbook.md says to).
    c = repo.push(CONFIG, "LOG_LEVEL=DEBUG\n", "Change a setting")
    assert repo.run().failed is None
    _assert_deploys(repo, c, b)


def test_a_rollback_by_pushing_an_older_deploy_commit_to_production(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    run = repo.run()
    deployed = repo.head("production")
    # The run's log names the deploy commit, to push back later.
    assert (
        f"Production now deploys {a} with the images sha-{a[:7]}: deploy commit {deployed}."
        in run.log
    )
    _git(repo.dev, "fetch", "--quiet", "origin")
    repo.push(CODE, "VERSION = 3\n", "Break the API")
    assert repo.run().failed is None

    # By hand, as docs/runbook.md shows: a clone that fetched production before has the earlier
    # deploy commit in its reflog.
    _git(repo.dev, "fetch", "--quiet", "origin")
    assert _git(repo.dev, "rev-parse", "origin/production@{1}") == deployed
    _git(repo.dev, "push", "--quiet", "--force", "origin", f"{deployed}:refs/heads/production")
    _assert_deploys(repo, a, a)

    # A fix on main deploys from there as usual. (A re-run of b's deploy would put b back.)
    c = repo.push(CODE, "VERSION = 4\n", "Fix the API")
    run = repo.run()
    assert run.failed is None, run.log
    assert "replacing it" not in run.log, "c is a later commit of a: an ordinary deploy"
    _assert_deploys(repo, c, c)


def test_a_run_started_by_hand_deploys_the_newest_commit(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    run = repo.run(dispatch=True)
    assert run.failed is None, run.log
    _assert_deploys(repo, a, a)


def test_a_commit_given_by_hand_must_be_on_main(repo: Repo) -> None:
    _git(repo.dev, "switch", "--quiet", "--create", "side")
    side = repo.push(CODE, "VERSION = 9\n", "Experiment")
    _git(repo.dev, "push", "--quiet", "origin", "side")
    main = repo.head("main")
    assert main

    run = repo.run(dispatch=True, commit=side, rollback=True)
    assert run.failed == "Find the source and image commits"
    assert f"Commit {side} isn't on main." in run.log
    run = repo.run(dispatch=True, commit="HEAD~1")
    assert run.failed == "Find the source and image commits"
    assert "must be a commit SHA" in run.log
    assert repo.head("production") is None


def test_nothing_is_deployed_unless_ci_passed(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    run = repo.run(ci_passed=False)
    assert run.failed == "Wait for CI on the source commit"
    assert f"CI did not pass on {a}, so it is not deployed." in run.log

    run = repo.run(ci_ran=False)
    assert run.failed == "Wait for CI on the source commit"
    assert f"No CI run found for {a}." in run.log
    assert repo.head("production") is None

    # By hand, a commit CI never ran on is deployed anyway.
    run = repo.run(dispatch=True, ci_ran=False)
    assert run.failed is None, run.log
    assert f"CI didn't run on {a}; deploying anyway" in run.log
    _assert_deploys(repo, a, a)


def test_a_push_to_production_while_deploying_is_not_overwritten(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    assert repo.run().failed is None
    b = repo.push(CONFIG, "LOG_LEVEL=DEBUG\n", "Change a setting")

    def push_by_hand() -> None:
        _git(repo.dev, "push", "--quiet", "--force", "origin", f"{a}:refs/heads/production")

    run = repo.run(before_step={"Commit and push to production": push_by_hand})
    assert run.failed == "Commit and push to production"
    assert "Pushing to production failed." in run.log
    assert repo.head("production") == a
    assert repo.head("main") == b


def test_a_deploy_that_changes_no_tag_still_records_its_source(repo: Repo) -> None:
    # Someone pinned main's tags to the images the deploy would set anyway.
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    pinned = repo.show(a, IMAGES).replace("newTag: 0.1.0", f"newTag: sha-{a[:7]}") + "\n"
    b = repo.push(IMAGES, pinned, "Pin the images")
    assert repo.run().failed is None
    production = repo.head("production")
    assert production
    assert _git(repo.origin, "diff", "--name-only", b, production) == ""
    assert _git(repo.origin, "log", "-1", "--format=%P|%s", production) == (
        f"{b}|Deploy {b[:7]} (images sha-{a[:7]}) [skip ci]"
    )


def test_a_production_branch_pushed_by_hand_is_compared_as_it_is(repo: Repo) -> None:
    a = repo.push(CODE, "VERSION = 2\n", "Change the API")
    b = repo.push(CODE, "VERSION = 3\n", "Change the API again")
    _git(repo.dev, "push", "--quiet", "origin", f"{b}:refs/heads/production")

    run = repo.run(sha=a)  # older than what production has
    assert run.failed is None, run.log
    assert f"Production already deploys {b}, which is {a} or a later commit" in run.log
    assert repo.head("production") == b

    c = repo.push(CONFIG, "LOG_LEVEL=DEBUG\n", "Change a setting")
    assert repo.run().failed is None
    _assert_deploys(repo, c, b)
