# ADR-0019: Building images in CI and deploying on push

- **Status:** Proposed
- **Date:** 2026-10-07; revised 2026-10-08: deploys go to a `production` branch that Argo CD
  follows, instead of commits to `main` ([why](#the-revision-of-2026-10-08))

## Context

Phase 7 is done when "a push to `main` deploys automatically" (brief §8). Two pieces were
missing:

- **Images.** The five service images come from one Dockerfile (ADR-0014) and must run on the
  production VM, which is **arm64**, and on the amd64 laptop. The laptop can't build arm64 images
  (no emulation), so they have to be built elsewhere and published to a registry the cluster
  pulls from without credentials.
- **Deploys.** Argo CD keeps the cluster in step with `deploy/k8s/overlays/production` on a
  branch of the repository (ADR-0017). The overlay pins each image's tag, so a new version only
  reaches production when something commits a new tag. ADR-0017 left that to image CI or Argo CD
  Image Updater.

The constraints:

- **$0 a month.** GitHub's native arm64 runners (`ubuntu-24.04-arm`) and GitHub Container
  Registry (GHCR) storage are free only for **public repositories**. The repository is **private
  for now**; making it public is the user's decision. In a private repository the same jobs would
  use up the account's free Actions minutes and 500 MB of package storage, and could be billed.
- **No secrets to manage.** Nothing should need a personal token, a deploy key, or a registry
  password, and the repository and its images will be public.
- **Production only runs what passed CI.** CI (`.github/workflows/ci.yml`) lints and tests every
  push to `main`.
- **Production never runs manifests ahead of their code.** A manifest can depend on new code: a
  probe that calls a new command, a setting the old code rejects. It must not go live with older
  images.
- **The node's memory is nearly all allocated** (ADR-0017: 8.97 of 9 GiB at limits), so the
  cluster can't take on another controller lightly.

### The revision of 2026-10-08

The first version of this decision had the workflow commit the new tags to `main`, which Argo CD
followed. A review found two ways it could pair manifests with the wrong images:

- **Manifests outran their images.** Argo CD deployed a push's manifest changes within its
  3-minute poll, while the image tags moved only after the build, the merge, and CI had finished
  (10 to 20 minutes), or never, if CI failed. A manifest that needed the new code ran against the
  old images in the meantime, and crash-looped.
- **The deploy commit could be ahead of its images.** The deploy job checked out the newest
  `main`, which could hold later commits whose images weren't built yet, and committed the tags on
  top of them.

## Decision

We will build and publish the images with a GitHub Actions workflow,
**`.github/workflows/images.yml`**, separate from CI, and deploy by having it push a **deploy
commit** to a **`production` branch**, which Argo CD's application follows and nothing else
writes. People edit the manifests on `main`, as before; the bot no longer commits to `main`. A
deploy commit is one commit of `main` (the **source commit**) with the image tags set to that
commit's images, made only once those images are published and CI has passed on it.

### When it runs

On a push to `main` that changes an **image input**, what goes into an image: `services/**`,
`libs/**`, `docker/**`, `pyproject.toml`, `uv.lock`, `.dockerignore`, `config/watchlist.toml`
(baked into the discovery image), or the workflow itself; or that changes **anything under
`deploy/`**, so a manifest change deploys too (through this workflow, the only way to
production). Changes to docs, the website, or Terraform alone don't start it.

It can also be started by hand (`workflow_dispatch`) on `main`, with two optional inputs:
`commit`, a commit of `main` to deploy instead of its newest, and `rollback`, which allows
deploying a commit older than the one production runs ([Rollbacks](#rollbacks)).

### Which images a commit runs

For the source commit S, the **image commit** I is the newest commit at or before S that changed
an image input (`git log -1 --format=%H S -- <image inputs>`), and its images are tagged
**`sha-<first 7 characters of I>`**. Nothing that goes into an image changed between I and S, so
building at S produces the same images as building at I; a test checks that the list of image
inputs covers everything the build reads (`.dockerignore`'s allowlist and the Dockerfile).

The workflow looks up the five `sha-<I>` images in GHCR (`docker buildx imagetools inspect`).
If all exist, as after a push that changed only manifests, the build is skipped and the deploy
reuses them. If any is missing, or can't be looked up, all five are built at S. This also covers
a run that never happened: GitHub keeps only the newest waiting run of the workflow, so when a
code push's run is replaced by a later push's, the later run finds the images missing and builds
them.

(For a merge that took its image inputs unchanged from the merged branch, git's history
simplification makes I the commit on that branch that last changed them. Its images are the
same, so the tag is as good as any.)

### The cost gate

**Every job has `if: github.event.repository.private == false`.** While the repository is
private, a run starts with every job skipped, so no runner starts and nothing is pushed or billed.
Making the repository public turns the workflow on with no other change. A test
(`tests/test_images_workflow.py`) fails if any job lacks the condition, or joins it with `||`.

### Four jobs

| Job | Runs on | What it does |
|---|---|---|
| `plan` | 1 job, `ubuntu-24.04` | Finds the source commit S (the pushed one, or the one given by hand, which must be on the branch), the image commit I, and the tag `sha-<I>`, and decides whether the images need building. |
| `build` | 10 jobs: 5 services × 2 architectures; `ubuntu-24.04` (amd64) and `ubuntu-24.04-arm` (arm64); only if images are missing | Builds `docker/service.Dockerfile` at S with `PACKAGE=filings-<service>` for its own architecture and pushes it to `ghcr.io/asood8/filings-<service>` **by digest**, without a tag. Saves the digest as a run artifact. |
| `merge` | 5 jobs, one per service, `ubuntu-24.04`; only if images are missing | Joins the two digests into one **multi-arch image** (an image index) and pushes it under the tags `sha-<I>` and `main`. Nothing is uploaded again; the registry already has both builds. |
| `deploy` | 1 job, `ubuntu-24.04`, only for `main`; also when the build was skipped, never after a failure | Waits for CI on S, then pushes S with the tags set to `sha-<I>` to `production`. |

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
  image's description. The `source` label also links each package to the repository. The
  `revision` is the image commit I, like the tag; the provenance records S, where it was built.
- **Attestations.** Each build carries a **provenance** attestation (`mode=max`: the build's
  inputs, build arguments, and the workflow run) and an **SBOM** (every package in the image),
  stored in the registry next to the image at no cost. They need no extra permissions or secrets.
  No build argument is secret.
- **Tags.** `sha-<first 7 characters of the image commit>` is what production pins, written only
  by this workflow, from that commit's image inputs. `main` moves to the newest build of `main`,
  for a quick look (`docker pull ghcr.io/asood8/filings-api:main`); production never uses it. A
  run started by hand with a `commit` given, or on another branch, doesn't move `main`; on another
  branch it publishes only the `sha-` tag and doesn't deploy.

### The deploy job

1. **Wait for CI on S.** It finds CI's push run for S (`gh run list --commit`) and waits for it
   (`gh run watch --exit-status`). CI runs alongside the builds and usually finishes first. If
   CI fails or is cancelled, the job fails and nothing is deployed; once CI passes (a re-run, say),
   re-running the deploy job deploys it. A run started by hand for a commit CI never ran on (one
   pushed with `[skip ci]`) deploys without it, with a warning.
2. **Check out S**, not the newest `main`, with the whole history.
3. **Read the production branch** (`git ls-remote`). Its head is normally a deploy commit, whose
   message names its source commit on a line `Source commit <SHA>`; a commit pushed there by hand
   without one is compared as it is. **If production already deploys S or a later commit of
   `main`, the job leaves it alone** with a notice, so a re-run of an older run never moves
   production backwards. A run started by hand with `rollback` deploys anyway. If there is no
   production branch yet, this deploy creates it.
4. **Set the tags.** `kustomize edit set image filings-<service>=*:sha-<I>` for each image, then
   it renders the production overlay to make sure it still builds. kustomize is a pinned,
   checksum-verified download, like the other tools (ADR-0017), at 5.8.1: the version inside
   kubectl v1.36.5, which `make k8s-check` renders with. Versions can render the same overlay
   differently (5.8.2 drops a label from the StatefulSets' `volumeClaimTemplates`), so the deploy
   job checks with the renderer CI validated. `tests/test_k8s_manifests.py` fails if the two
   versions part.
5. **Commit on top of S and push to production**, as `github-actions[bot]`:
   `Deploy <short S> (images sha-<short I>) [skip ci]`, with a body of three lines,
   `Source commit <S>`, `Image commit <I>`, and `Run <the run's URL>`, and no `Key: value`
   trailers. The commit is made even when the tags were already right, so production always
   records its source. The push is
   `git push --force-with-lease=refs/heads/production:<the head read in step 3> origin HEAD:refs/heads/production`.
   It has to force, because the new deploy commit sits on S, not on the previous deploy commit;
   the lease refuses the push if production moved since step 3 (someone's rollback by hand), and
   the job then fails rather than overwrite it. The job's log names the new deploy commit.

Argo CD polls the repository every 3 minutes, sees `production` move, and rolls out the change.

**Production's tip is S's tree plus the five tags.** Its history is `main`'s up to S, then the
deploy commit. Earlier deploy commits drop off the branch when the next deploy replaces them;
each Images run's log names the one it pushed, and Argo CD's history keeps the last five
revisions it deployed.

**The tags live in their own file**, `deploy/k8s/overlays/production/images/kustomization.yaml`,
a Kustomize **component** that the production overlay includes. `kustomize edit` rewrites the
whole file it edits in its own layout (indentation, key order, comment placement), which would
have reformatted the hand-written overlay on the first deploy. The component holds only the image
list, already in that layout, so each deploy changes exactly the five `newTag` lines. The rendered
production manifests are byte-for-byte the same as before the split. On `main` the tags stay a
placeholder (`0.1.0`) that doesn't exist in GHCR; only deploy commits set real ones.

### The guarantees, and why they hold

- **A manifest never runs ahead of its code.** A deploy commit is S's tree with the images of
  S's own image inputs; Argo CD sees S's manifests only in that commit, made after the images
  exist.
- **Only commits that passed CI** reach production (or, started by hand, a commit CI skipped).
- **Never backwards by accident.** An older run, or a re-run, leaves a newer deploy alone; going
  back takes a run started by hand with `rollback`, or a push by hand.
- **No loops.** The workflow runs only for pushes to `main`, and the deploy pushes only to
  `production`. No workflow in the repository runs for pushes to `production` (CI, the audit, and
  this one filter on `main`; a test checks every workflow). On top of that, GitHub starts no
  workflow for a push made with `GITHUB_TOKEN`, and the deploy commit's message has `[skip ci]`.
- **One run at a time, in push order.** The workflow's concurrency group queues a newer run
  behind the running one instead of cancelling it (`cancel-in-progress: false`). A run is never
  stopped between publishing images and deploying. GitHub keeps only the newest waiting run; the
  ones it replaces are older commits of the same `main`, so the newer run deploys their changes
  too (and builds what they would have). A run started by hand shares the queue, so it can
  replace a waiting push's run, or be replaced by one; start it again after.

### Rollbacks

- **Deploy an older commit:** **Actions → Images → Run workflow** on `main`, with `commit` set to
  the commit to go back to and `rollback` ticked. Its images are normally still in GHCR (it keeps
  every version), so nothing is built; if they are gone, they are built again from that commit
  (without moving the `main` tag). CI must have passed on that commit. It takes a few minutes.
- **Push an older deploy commit back:** `git push --force origin <deploy commit>:production`. Its
  SHA is in that deploy's Images run log, in `git reflog origin/production` of a clone that
  fetched it, and in Argo CD's history. Nothing runs in Actions; Argo CD picks it up within
  3 minutes.
- **Either way, fix `main` next** (revert, or fix forward): the next push deploys `main`'s newest
  code again. The next deploy goes on from the rolled-back commit as usual.
- **`git revert` on `main` alone** also rolls back, but a reverted code change is a new image
  input, so it waits for a full build.

`docs/runbook.md` has the commands, and what to do when the newer commit added a migration.

### Permissions

The workflow uses only the built-in `GITHUB_TOKEN`, which is created for each job and expires
with it. The default is read-only; each job asks for what it needs:

| Job | Permissions |
|---|---|
| `plan` | `contents: read`, `packages: read` (look the images up, also while a package is private) |
| `build` | `contents: read`, `packages: write` |
| `merge` | `contents: read` (metadata-action reads the repository's details), `packages: write` |
| `deploy` | `contents: write` (push to `production`), `actions: read` (CI's result) |

The plan and build jobs don't keep git credentials in the checkout (`persist-credentials: false`).
Tests fail if any job other than `deploy` can write to the repository, or if any script pushes
anywhere but `production`.

### Turning it on

When the user makes the repository public:

1. The next push that touches the paths above, or **Actions → Images → Run workflow** on `main`,
   runs it. The first run creates the five GHCR packages, linked to the repository, and the
   `production` branch.
2. **Make each package public** (on GitHub: the package's **Package settings → Change
   visibility**), unless GHCR already shows it as public. The cluster then pulls without
   credentials, and public package storage is free; private packages count against the 500 MB
   free quota. It is a one-time setting per package.

**On a new cluster,** the VM's bootstrap applies `deploy/argocd` from `main` (ADR-0018), as
before. If `production` doesn't exist yet, the application reports that it can't find the
branch, and finds it on a later refresh once the first deploy has run; the Sealed Secrets
controller follows `main` and installs regardless. The branch isn't created by hand from `main`
(`git push origin main:production`): `main`'s tags are the placeholder, so nothing could start.
Running the workflow by hand creates it properly.

`main` can now get branch protection or a ruleset freely; the bot doesn't push there. A rule on
`production` must still let `GITHUB_TOKEN` force-push to it, or deploys stop.

### How this was tested

GitHub's runners, GHCR, and the Actions APIs can't run on the laptop, so the pieces were tested
separately:

- **actionlint 1.7.12** with **shellcheck 0.11.0** (the pre-commit hook, also in CI), which
  checks the `run:` scripts: no findings. When it was introduced, a copy of the workflow with three
  planted mistakes (a misspelled runner label, an unquoted variable in a script, a misspelled
  `upload-artifact` input) got all three reported, so the clean result means something.
- **The plan and deploy jobs' scripts, as written in the workflow,** are run by
  `tests/test_images_workflow.py` under `bash -e -o pipefail`, like a runner runs them, against a
  throwaway git repository (a bare "origin" standing in for GitHub and a developer's clone, in a
  temporary directory deleted after each test), with stubs for `gh` (CI's result), `docker` (the
  registry), and `kustomize`. Each step gets the environment and `${{ }}` values the workflow
  gives it, its outputs feed the later steps, and steps run or skip by their `if:`. The scenarios:
  a first deploy (no production branch); a manifest-only push reusing the images; a code push
  building new ones; a run for a commit after which `main` moved on; images a replaced run never
  built; an older run re-run (left alone); a rollback started by hand, and the push after it; a
  rollback by pushing an older deploy commit back; a run started by hand; a commit given by hand
  that isn't on `main`, or isn't a SHA; CI failing or never running; production pushed to while
  a deploy runs (the lease refuses); a deploy that changes no tag; and a production branch pushed
  by hand.
- **The same scenarios with the real kustomize 5.8.1** (checksum-verified) instead of the stub,
  and the whole `deploy/` tree in the throwaway repository, rendering production after each: all
  passed, and each deploy changed exactly the five `newTag` lines, comments kept.
- **The rules** failed, each time on the intended tests, for copies with one thing broken: in the
  workflow, no skip for older runs, the rollback input ignored, a plain force push instead of the
  lease, a push to `main`, the image commit taken as the source commit, the deploy checking out
  `main`, no `[skip ci]`, no `--allow-empty`, a commit given by hand not checked against the
  branch, the registry lookup always finding the images, the source line ignored, no `deploy/**`
  trigger, a CI failure ignored, a job without the gate, and a plan job that can write; elsewhere,
  the application following `main`, the Sealed Secrets controller following `production`, and CI
  or the audit running for pushes to `production`.
- **From the first version (2026-10-07):** the merge step with a stub `docker` (every tag and
  annotation arrived as one argument; one digest instead of two failed with its error); the image
  pipeline on one architecture, with a throwaway `docker-container` builder and local registry: a
  build with build-push-action's arguments pushed by digest, with provenance (`mode=max`) and an
  SBOM, then `docker buildx imagetools create` with the merge step's arguments made an OCI image
  index under both tags, with the annotations and the attestations. The `gh` flags the deploy job
  uses exist in gh 2.102.0.
- **The manifests:** `make k8s-check` passes, and kustomize 5.8.1 renders the production overlay
  the same as `kubectl kustomize`.

**The gate itself** shows on the first push after this workflow lands, while the repository is
private: an "Images" run appears in the Actions tab with every job skipped and no minutes used.

**Not tested until the first run:** the arm64 build on a real runner, logging in to GHCR and
pushing with `GITHUB_TOKEN`, the registry lookup against GHCR, the `gha` cache, `gh run list` and
`gh run watch` against the API, the force push to `production` with `GITHUB_TOKEN`, and Argo CD
following the branch. To check the first run: every job is green;
`docker buildx imagetools inspect ghcr.io/asood8/filings-api:main` lists `linux/amd64` and
`linux/arm64`; `production` has the bot's deploy commit on top of the pushed commit; Argo CD
shows that commit and the new tags. Then push a manifest-only change: its run skips the build and
deploys with the same tags.

## Alternatives considered

- **Committing the tags to `main`** (this ADR's first decision, 2026-10-07), with Argo CD
  following `main`. Simple, and the deploy history was `main`'s history, but Argo CD deployed
  manifest changes before their images existed, and the deploy commit went on the newest `main`,
  which could be ahead of the images (see [the revision](#the-revision-of-2026-10-08)). It also
  put a bot commit on `main` after every deploy (pull before pushing again) and broke if `main`
  got branch protection.
- **Argo CD Image Updater** (a controller that watches the registry for new tags and updates the
  application). Argo CD would still follow `main`'s manifests, so they would still go live before
  their images; Image Updater only moves tags. With its git write-back, the cluster would hold a
  credential that can push to the repository, and another controller would need memory the node
  doesn't have. Without write-back, git would no longer say what runs. It also can't wait for CI.
- **Promotion by pull request:** each deploy opens a pull request with the new tags (to `main`,
  or to `production`), merged by hand or by auto-merge once checks pass. It adds a step per deploy
  for a one-person project, and pull requests opened with `GITHUB_TOKEN` start no workflows, so
  their checks need a personal or GitHub App token, a long-lived secret. If deploys should wait
  for a human, a GitHub environment with a required reviewer on the deploy job adds that without
  pull requests.
- **Chaining the deploy commits** (each deploy commit with the previous one as a second parent,
  so `production` only fast-forwards and keeps every deploy in its history). No force push, but
  merge commits whose tree ignores one parent are confusing to read, and a rollback is still a new
  commit. The run logs and Argo CD's history already record the deploys. It can be added later.
- **Keeping Argo CD on `main` and holding manifests back otherwise:** manual syncs, or sync
  windows, give up automatic deploys; requiring a manifest change to wait for a separate commit
  after its images exist puts the ordering on people, which is what failed.
- **arm64 under QEMU emulation on amd64 runners.** Works in a private repository's free minutes,
  but emulated builds run several times slower, and the native arm64 runners cost nothing once the
  repository is public.
- **One job per service building both architectures** (`platforms: linux/amd64,linux/arm64`).
  It needs emulation for the second architecture. Building by digest on native runners and joining
  the results is the pattern Docker documents for multi-runner builds.
- **Building on every push to `main`**, without finding the image commit. A manifest-only push
  would rebuild all five images (10 jobs, several minutes) for identical content.
- **A moving tag (`main`) with `imagePullPolicy: Always` and restarts.** Git wouldn't change
  when the images do, so Argo CD wouldn't know to roll out, and nothing would record what runs.
- **Pinning digests instead of tags in the overlay** (`kustomize edit set image ...@sha256:...`).
  A digest can't be moved, unlike a tag, but it is unreadable and differs per service. A `sha-`
  tag is only ever written by this workflow, from that commit's image inputs. Digests can be added
  later if that guarantee matters.
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
- **What runs is on `production`, not `main`.** `main`'s overlay keeps placeholder tags; the
  deployed ones are in `git show origin/production:deploy/k8s/overlays/production/images/kustomization.yaml`,
  and the deployed source commit is the `Source commit` line of `git log -1 origin/production`.
  The bot never commits to `main`, so there is nothing to pull after a deploy.
- **A manifest change goes live after CI passes** (several minutes), not within Argo CD's
  3-minute poll. In return, everything production runs passed CI.
- **Every image build rebuilds and redeploys all five services**, even when only one changed: an
  image's labels (commit, build time) change its digest anyway. The cache makes an unchanged
  service's build quick, and the rollout restarts each service once. Per-service change detection
  could come later. A manifest-only push builds nothing.
- **A failed or cancelled CI run blocks that commit's deploy** until CI passes on it. CI cancels
  an in-progress run on `main` when a newer push arrives (its concurrency group), so that older
  commit's deploy fails; the newer commit deploys if it also changed an image input or a manifest.
  Otherwise, re-run CI on the older commit, then its deploy job, or let CI's concurrency stop
  cancelling runs on `main`.
- **`production` is force-pushed on every deploy.** Old deploy commits leave the branch; the
  record of deploys is the Images runs and Argo CD's history, and the deploy commits stay
  reachable by SHA only as long as GitHub keeps them.
- **GHCR keeps every version** (per image commit and service: two images, two attestations, one
  index). Public storage is free; a cleanup job (`actions/delete-package-versions`) can prune old
  `sha-` tags if the list gets long, but should keep the ones a rollback may need (a pruned one is
  rebuilt when deployed again).
- **Rollbacks** are a run started by hand for an older commit, or an older deploy commit pushed
  back to `production` ([Rollbacks](#rollbacks)), followed by a fix on `main`.
- **Actions are pinned by major version**, as in CI. A compromised tag of a third-party action
  would run in a job that can push packages or push to `production`; pinning to commit SHAs (with
  Renovate or Dependabot to update them) is stronger and can come later.
- **Revisit** if the repository stays private (QEMU on amd64 runners within the free minutes, or
  building elsewhere), if a second environment appears (a `staging` branch and overlay, deployed
  the same way), or if deploys should wait for a human (a GitHub environment with a reviewer).

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
- **Deploy branch (environment branch):** a branch that holds exactly what one environment
  runs, moved only by automation once a version is ready. Argo CD follows it instead of `main`,
  so `main` can hold work whose images aren't built yet. A deploy is a push to the branch, and a
  rollback is pushing an older version to it.
- **`git push --force-with-lease`:** a force push that only happens if the branch on the server
  is still at the commit you expect. It replaces the branch's history like `--force`, but refuses
  instead of silently discarding something someone else pushed in the meantime.
- **Argo CD Image Updater:** an add-on that watches a registry and updates the deployed tags
  itself, an alternative to having CI set them (rejected above).
- **Kustomize component:** a reusable piece of Kustomize configuration that an overlay includes
  under `components:`. Here it holds only the image tags, so tools can rewrite it without
  touching the rest of the overlay.
- **actionlint:** a checker for GitHub Actions workflows. It catches unknown keys, bad
  expressions, wrong action inputs, and, with shellcheck, mistakes in the shell scripts.
