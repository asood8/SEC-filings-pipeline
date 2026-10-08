# ADR-0019: Building images in CI and deploying on push

- **Status:** Proposed
- **Date:** 2026-10-07

## Context

Phase 7 is done when "a push to `main` deploys automatically" (brief §8). Two pieces were
missing:

- **Images.** The five service images come from one Dockerfile (ADR-0014) and must run on the
  production VM, which is **arm64**, and on the amd64 laptop. The laptop can't build arm64 images
  (no emulation), so they have to be built elsewhere and published to a registry the cluster
  pulls from without credentials.
- **Deploys.** Argo CD keeps the cluster in step with `deploy/k8s/overlays/production` on `main`
  (ADR-0017). The overlay pins each image's tag, so a new version only reaches production when
  something commits a new tag. ADR-0017 left that to image CI or Argo CD Image Updater.

The constraints:

- **$0 a month.** GitHub's native arm64 runners (`ubuntu-24.04-arm`) and GitHub Container
  Registry (GHCR) storage are free only for **public repositories**. The repository is **private
  for now**; making it public is the user's decision. In a private repository the same jobs would
  use up the account's free Actions minutes and 500 MB of package storage, and could be billed.
- **No secrets to manage.** Nothing should need a personal token, a deploy key, or a registry
  password, and the repository and its images will be public.
- **Production only runs what passed CI.** CI (`.github/workflows/ci.yml`) lints and tests every
  push to `main`.
- **The node's memory is nearly all allocated** (ADR-0017: 8.97 of 9 GiB at limits), so the
  cluster can't take on another controller lightly.

## Decision

We will build and publish the images with a GitHub Actions workflow,
**`.github/workflows/images.yml`**, separate from CI, and deploy by having it **commit the new
tags to `main`** (GitOps commit-back). Argo CD does the rest.

### When it runs

On a push to `main` that changes what goes into an image: `services/**`, `libs/**`, `docker/**`,
`pyproject.toml`, `uv.lock`, `.dockerignore`, `config/watchlist.toml` (baked into the discovery
image), or the workflow itself. It can also be started by hand (`workflow_dispatch`). Changes to
docs, the website, Terraform, or the manifests don't build images.

### The cost gate

**Every job has `if: github.event.repository.private == false`.** While the repository is
private, a run starts with every job skipped, so no runner starts and nothing is pushed or billed.
Making the repository public turns the workflow on with no other change. A test
(`tests/test_images_workflow.py`) fails if any job lacks the condition, or joins it with `||`.

### Three jobs

| Job | Runs on | What it does |
|---|---|---|
| `build` | 10 jobs: 5 services × 2 architectures; `ubuntu-24.04` (amd64) and `ubuntu-24.04-arm` (arm64) | Builds `docker/service.Dockerfile` with `PACKAGE=filings-<service>` for its own architecture and pushes it to `ghcr.io/asood8/filings-<service>` **by digest**, without a tag. Saves the digest as a run artifact. |
| `merge` | 5 jobs, one per service, `ubuntu-24.04` | Joins the two digests into one **multi-arch image** (an image index) and pushes it under the tags `sha-<short commit SHA>` and `main`. Nothing is uploaded again; the registry already has both builds. |
| `deploy` | 1 job, `ubuntu-24.04`, only for `main` | Waits for CI on the same commit, sets the production overlay's tags to `sha-<short commit SHA>`, and commits that to `main`. |

- **Native builds, no emulation.** Each architecture builds on a runner of that architecture,
  as fast as an amd64 build. Every dependency installs from a prebuilt aarch64 wheel (ADR-0014),
  so the arm64 build does the same work as the amd64 one.
- **Actions** at their current major versions in October 2026: `docker/setup-buildx-action@v4`,
  `docker/login-action@v4`, `docker/metadata-action@v6`, `docker/build-push-action@v7`,
  `actions/checkout@v7`, `actions/upload-artifact@v7`, `actions/download-artifact@v8`. CI pins
  actions the same way, by major version.
- **Build context.** The checkout, not build-push-action's default Git context, so
  `.dockerignore` (an allowlist, ADR-0014) decides what the build sees.
- **Layer cache** in the GitHub Actions cache (`type=gha`, free, 10 GB per repository), one scope
  per service and architecture so the ten builds don't overwrite each other's. A code-only change
  reuses the dependency layer.
- **Labels and annotations.** `docker/metadata-action` adds the OCI labels (`source`,
  `revision`, `created`, `version`, `licenses`, `url`), with a per-service title and
  description; the index gets the same as annotations, which is where GHCR reads a multi-arch
  image's description. The `source` label also links each package to the repository.
- **Attestations.** Each build carries a **provenance** attestation (`mode=max`: the build's
  inputs, build arguments, and the workflow run) and an **SBOM** (every package in the image),
  stored in the registry next to the image at no cost. They need no extra permissions or secrets.
  No build argument is secret.
- **Tags.** `sha-<first 7 characters of the commit>` is what production pins: one per commit,
  written only by this workflow. `main` moves to the newest build of `main`, for a quick look
  (`docker pull ghcr.io/asood8/filings-api:main`); production never uses it. A run started by hand
  on another branch publishes only the `sha-` tag and doesn't deploy.

### The deploy job

1. **Wait for CI.** It finds CI's run for the same commit (`gh run list --commit`) and waits for
   it (`gh run watch --exit-status`). CI runs alongside the builds and usually finishes first. If
   CI fails or is cancelled, the job fails and nothing is deployed; once CI passes (a re-run, say),
   re-running the deploy job deploys it.
2. **Set the tags.** It checks out the newest commit of `main` (which may be newer than the
   build's) and runs `kustomize edit set image filings-<service>=*:sha-<short SHA>` for each image,
   then renders the production overlay to make sure it still builds. kustomize is a pinned,
   checksum-verified download, like the other tools (ADR-0017), at 5.8.1: the version inside
   kubectl v1.36.5, which `make k8s-check` renders with. Versions can render the same overlay
   differently (5.8.2 drops a label from the StatefulSets' `volumeClaimTemplates`), so the deploy
   job checks with the renderer CI validated. `tests/test_k8s_manifests.py` fails if the two
   versions part.
3. **Commit and push**, only if something changed, as `github-actions[bot]`:
   `Deploy sha-<short SHA> to production [skip ci]`, with the full commit SHA and the run's URL in
   the body. If `main` moved meanwhile, it rebases the commit onto it and pushes again, up to
   three times. A conflict means someone changed the tags in the meantime (a rollback, say); the
   job then fails rather than overwrite them.

Argo CD polls the repository every 3 minutes, sees the commit, and rolls out the five services.

**The tags live in their own file**, `deploy/k8s/overlays/production/images/kustomization.yaml`,
a Kustomize **component** that the production overlay includes. `kustomize edit` rewrites the
whole file it edits in its own layout (indentation, key order, comment placement), which would
have reformatted the hand-written overlay on the first deploy. The component holds only the image
list, already in that layout, so each deploy changes exactly the five `newTag` lines. The rendered
production manifests are byte-for-byte the same as before the split.

### No loops, no races

- **No loops.** The deploy commit can't start the workflow again, three ways over: GitHub starts
  no workflow for a push made with `GITHUB_TOKEN`; the commit changes only `deploy/`, which isn't
  in the path filter; and `[skip ci]` in the message skips push-triggered workflows. The test
  checks the last two.
- **One run at a time.** The workflow's concurrency group queues a newer push behind the running
  one instead of cancelling it (GitHub keeps only the newest waiting run). A run is never stopped
  between publishing images and committing tags, and deploys land in push order.

### Permissions

The workflow uses only the built-in `GITHUB_TOKEN`, which is created for each job and expires
with it. The default is read-only; each job asks for what it needs:

| Job | Permissions |
|---|---|
| `build` | `contents: read`, `packages: write` |
| `merge` | `contents: read` (metadata-action reads the repository's details), `packages: write` |
| `deploy` | `contents: write` (push to `main`), `actions: read` (CI's result) |

The build jobs don't keep git credentials in the checkout (`persist-credentials: false`). A test
fails if any job other than `deploy` can write to the repository.

### Turning it on

When the user makes the repository public:

1. The next push that touches the paths above, or **Actions → Images → Run workflow** on `main`,
   runs it. The first run creates the five GHCR packages, linked to the repository.
2. **Make each package public** (on GitHub: the package's **Package settings → Change
   visibility**), unless GHCR already shows it as public. The cluster then pulls without
   credentials, and public package storage is free; private packages count against the 500 MB
   free quota. It is a one-time setting per package.
3. Until the first deploy, the overlay's tag is a placeholder (`0.1.0`) that doesn't exist in
   GHCR, so nothing could have been deployed from it.

If `main` ever gets branch protection or a ruleset that requires pull requests, the deploy job's
push is refused; either allow GitHub Actions to bypass it or switch the job to opening a pull
request (alternatives below).

### How this was tested

GitHub's runners, GHCR, and the Actions APIs can't run on the laptop, so the pieces were tested
separately:

- **actionlint 1.7.12** (official release, checksum-verified) with **shellcheck 0.11.0**, which
  checks the `run:` scripts: no findings in `images.yml` or `ci.yml`. A copy of `images.yml` with
  three planted mistakes (a misspelled runner label, an unquoted variable in a script, a misspelled
  `upload-artifact` input) got all three reported, so the clean result means something. It knows
  `ubuntu-24.04-arm` and the inputs of the Docker and artifact actions at these major versions.
- **The shell steps, as written in the workflow,** extracted from the YAML and run locally under
  `bash -e -o pipefail`, like a runner runs them:
  - The merge step, with a stub `docker`: every tag and annotation (one with spaces) arrived as
    one argument; with one digest instead of two, or without `jq`, it failed with its error.
  - The tag step, with the real kustomize 5.8.1: from the placeholder it changed exactly the five
    `newTag` lines (the file's comments survive `kustomize edit`); setting the same tag again
    changed nothing; a re-run for the deployed commit, or for an older one, left the file alone;
    a parent commit's tags, mixed tags, or a tag of an unknown commit were replaced.
  - The wait-for-CI step, with a stub `gh`: it deploys when CI passes (also when CI's run appears
    late), fails when CI fails or never appears, and on a manual run deploys a commit CI skipped.
    The `gh` flags it uses exist in gh 2.102.0.
- **The image pipeline on one architecture:** `make image-parser` built the parser image. Then,
  with a throwaway `docker-container` builder (what `setup-buildx-action` creates) and a throwaway
  local registry, both removed afterwards: a build with build-push-action's arguments pushed by
  digest (the registry had no tags), with provenance (`mode=max`) and an SBOM; then `docker buildx
  imagetools create` with the merge step's tag and `index:` annotation arguments. The result was
  an OCI image index under both tags, carrying the annotations, the amd64 image, and its
  attestation manifest; the provenance recorded `PACKAGE=filings-parser`, and the SBOM listed the
  Python packages (confluent-kafka, psycopg-binary, filings-parser, ...).
- **The manifests:** the production overlay renders byte-for-byte as before the component split,
  kustomize 5.8.1 renders it the same as `kubectl kustomize`, and `make k8s-check` passes.
- **The rules:** `tests/test_images_workflow.py` failed, each time on the intended test, for
  copies of the workflow with one rule broken: a job without the gate, the gate joined with `||`,
  arm64 on an amd64 runner, a service missing, a build job allowed to write to the repository, a
  `deploy/` path in the trigger, and no `[skip ci]`.

**The gate itself** shows on the first push after this workflow lands, while the repository is
private: an "Images" run appears in the Actions tab with every job skipped and no minutes used.

**Not tested until the first run:** the arm64 build on a real runner, logging in to GHCR and
pushing with `GITHUB_TOKEN`, the `gha` cache, `gh run list` and `gh run watch` against the API,
and the push to `main`. To check the first run: every job is green;
`docker buildx imagetools inspect ghcr.io/asood8/filings-api:main` lists `linux/amd64` and
`linux/arm64`; `main` has the bot's deploy commit; Argo CD shows the new tags.

## Alternatives considered

- **arm64 under QEMU emulation on amd64 runners.** Works in a private repository's free minutes,
  but emulated builds run several times slower, and the native arm64 runners cost nothing once the
  repository is public.
- **One job per service building both architectures** (`platforms: linux/amd64,linux/arm64`).
  It needs emulation for the second architecture. Building by digest on native runners and joining
  the results is the pattern Docker documents for multi-runner builds.
- **Argo CD Image Updater** (a controller that watches the registry for new tags and updates the
  application). With its git write-back, the cluster would hold a credential that can push to the
  repository, and another controller would need memory the node doesn't have. Without write-back,
  git would no longer say what runs, so a revert couldn't roll back. It also couldn't wait for CI.
  Committing from CI needs no credential outside the job, and each deploy is a commit in `main`'s
  history.
- **A moving tag (`main`) with `imagePullPolicy: Always` and restarts.** Git wouldn't change
  when the images do, so Argo CD wouldn't know to roll out, and nothing would record what runs.
- **Pinning digests instead of tags in the overlay** (`kustomize edit set image ...@sha256:...`).
  A digest can't be moved, unlike a tag, but it is unreadable and differs per service. A `sha-`
  tag is only ever written by this workflow, from that commit. Digests can be added later if that
  guarantee matters.
- **Opening a pull request for each deploy** instead of pushing to `main`. That adds a click (or
  auto-merge) per deploy for a one-person project. It is the way to go if `main` gets protection
  rules.
- **A personal access token or GitHub App token for the push.** Pushes made with them do start
  workflows (a loop risk the guards above would then have to stop alone), and they are long-lived
  secrets to store and rotate. `GITHUB_TOKEN` is neither.
- **Building after CI** (`workflow_run`). Images would build only after tests pass, at the cost
  of running the two in series, and path filters don't apply to `workflow_run`. Building in
  parallel and gating only the deploy is faster and deploys the same commits.
- **GitHub artifact attestations** (`actions/attest-build-provenance`, signed with Sigstore). Also
  free for public repositories and verifiable with `gh attestation verify`, but they need the
  `id-token: write` and `attestations: write` permissions. BuildKit's attestations need neither;
  GitHub's can be added later.
- **yq instead of `kustomize edit`.** yq keeps comments but drops blank lines from the file it
  edits. `sed` on the `newTag:` lines would work but breaks silently if the layout changes.
- **Docker Hub.** Its free plan limits pulls and private repositories; GHCR is free for public
  images and accepts `GITHUB_TOKEN`.

## Consequences

- **Nothing runs while the repository is private**, and arm64 images can't be built on the
  laptop, so production can't be deployed before the repository is public.
- **After each deploy, `main` on GitHub is a commit ahead** of a local checkout: pull before
  pushing again. The bot's commits are part of `main`'s history, one per deploy.
- **Every image build rebuilds and redeploys all five services**, even when only one changed: an
  image's labels (commit, build time) change its digest anyway. The cache makes an unchanged
  service's build quick, and the rollout restarts each service once. Per-service change detection
  could come later.
- **A failed or cancelled CI run blocks that commit's deploy** until CI passes on it. CI cancels
  an in-progress run on `main` when a newer push arrives (its concurrency group), so that older
  commit's deploy fails; the newer commit deploys if it also changed an image path. Otherwise,
  re-run CI on the older commit, then its deploy job, or let CI's concurrency stop cancelling runs
  on `main`.
- **GHCR keeps every version** (per commit and service: two images, two attestations, one index).
  Public storage is free; a cleanup job (`actions/delete-package-versions`) can prune old `sha-`
  tags if the list gets long.
- **Rollbacks are commits:** revert the bot's deploy commit, or set an older `sha-` tag in the
  images file, and push (`deploy/k8s/README.md`).
- **Actions are pinned by major version**, as in CI. A compromised tag of a third-party action
  would run in a job that can push packages or commit to `main`; pinning to commit SHAs (with
  Renovate or Dependabot to update them) is stronger and can come later.
- **Revisit** if the repository stays private (QEMU on amd64 runners within the free minutes, or
  building elsewhere), if a second environment appears (a staging overlay with its own tag
  component), or if deploys should wait for a human (pull requests).

## Tools in plain language

- **GHCR (GitHub Container Registry):** GitHub's registry for container images, at `ghcr.io`.
  Images live next to the repository, the workflow logs in with its own `GITHUB_TOKEN`, and public
  images are free to store and to pull.
- **`GITHUB_TOKEN`:** a credential GitHub creates for each workflow job, with only the permissions
  the workflow asks for, and which expires when the job ends.
- **Native arm64 runners:** GitHub-hosted virtual machines with Arm processors
  (`ubuntu-24.04-arm`). An arm64 image builds there the same way an amd64 image builds on an
  ordinary runner, with no emulation. They are free for public repositories.
- **Digest:** the SHA-256 hash of an image's manifest, such as `sha256:be19...`. It identifies one
  exact image and can never point at anything else, unlike a tag, which is a movable name.
- **Pushing by digest:** uploading an image to the registry without giving it a tag. Each build
  job pushes its architecture's image this way and passes the digest on; only the merge step puts
  tags on the finished multi-arch image.
- **Multi-arch image (image index or manifest list):** a small document listing one image per
  CPU architecture. A tag points at the index, and each machine pulls the image that matches it:
  the cluster gets arm64, the laptop amd64.
- **Provenance and SBOM attestations:** documents attached to an image. Provenance records how,
  where, and from what it was built; an SBOM (software bill of materials) lists every package in
  it, so vulnerability scanners can check it without running it.
- **GitOps commit-back:** CI deploys by committing a change to the manifests in git, here the
  image tags, rather than talking to the cluster. Argo CD, which keeps the cluster matching git,
  rolls it out. Git history is the deploy history, and a revert is a rollback.
- **Argo CD Image Updater:** an add-on that watches a registry and updates the deployed tags
  itself, the alternative to commit-back (rejected above).
- **Kustomize component:** a reusable piece of Kustomize configuration that an overlay includes
  under `components:`. Here it holds only the image tags, so tools can rewrite it without
  touching the rest of the overlay.
- **actionlint:** a checker for GitHub Actions workflows. It catches unknown keys, bad
  expressions, wrong action inputs, and, with shellcheck, mistakes in the shell scripts.
