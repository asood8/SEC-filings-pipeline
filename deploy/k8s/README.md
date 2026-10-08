# Kubernetes manifests

The production system on one k3s node (Oracle Cloud Ampere A1: arm64, 2 OCPU, 12 GB), as
Kustomize directories that Argo CD keeps in sync with `main`. Why it is built this way, the
resource budget, and the security posture are in
[ADR-0017](../../docs/adr/0017-kubernetes-manifests.md).

Nothing here has been applied to a cluster yet: the cluster and the cloud accounts are created in
Phase 7. Everything renders and validates without one.

Operating it (alerts, dead letters, deploys and rollbacks, rotating secrets, backups and restores,
capacity) is in the [runbook](../../docs/runbook.md).

```
deploy/
  k8s/
    base/                  the whole application, environment-neutral (namespace "filings")
    overlays/production/   replica counts, production settings, and (Phase 7) the SealedSecrets
                           in sealed-secrets/
      images/              the service images' registry and tags, which CI updates on each deploy
    platform/sealed-secrets/   the Sealed Secrets controller (vendored upstream release)
    secrets-example/       EXAMPLE Secrets with placeholder values: the list of every secret
    secrets-local/         your filled-in copies before sealing (gitignored; never commit)
  argocd/
    install/               Argo CD itself (pinned upstream release, trimmed)
    project.yaml, filings.yaml, sealed-secrets.yaml   what Argo CD deploys
```

## Render and validate

```
make k8s-tools   # once: pinned, checksum-verified kubectl, kubeconform, kubeseal in ~/.local/bin
make k8s-check   # render every directory, validate against the Kubernetes API schemas, check rules
kubectl kustomize deploy/k8s/overlays/production   # print what Argo CD would apply
```

`make k8s-check` needs no cluster, but it needs the internet: kubeconform downloads the JSON
schemas (cached in `~/.cache/kubeconform`), and `deploy/argocd/install` fetches Argo CD's release
manifest. CI runs it on every push. The project's own rules (requests and limits on every
container, the memory budget, pod security, pinned images, real setting names) are tests in
`tests/test_k8s_manifests.py`, which `make test` also runs when kubectl is installed.

## Configuration

- **Settings** are environment variables with the names from
  `libs/common/src/filings_common/config.py` (and the discovery and notifier settings classes).
  Non-secret ones are in `base/config.env`, with production values merged over them from
  `overlays/production/config.env`. Both become one ConfigMap whose name carries a hash of its
  content, so changing a setting rolls out the pods that use it.
- **`DATABASE_URL`** is assembled in each workload from `POSTGRES_DB` (config) and the postgres
  Secret, so the password has one source.
- **Images** are `ghcr.io/asood8/filings-<service>`, multi-arch (ADR-0014), built by CI. The
  production overlay pins their tag in `overlays/production/images/kustomization.yaml`, which CI
  updates ([Deploying](#deploying)). The GHCR packages must be public (free), or the cluster needs
  a pull secret.

## Secrets

Secrets are committed only as **SealedSecrets**: encrypted with the cluster's public key, so the
repository can be public. Only the Sealed Secrets controller in the cluster holds the private key
and turns them back into ordinary Secrets.

| Secret | Keys | Used by |
|---|---|---|
| `postgres` | `POSTGRES_USER`, `POSTGRES_PASSWORD` (URL-safe: `openssl rand -hex 32`) | postgres, every service, migrate, backups |
| `mongodb` | `MONGODB_URL` (Atlas `mongodb+srv://...`) | parser, summarizer |
| `r2-raw-filings` | `OBJECT_STORE_S3_ENDPOINT_URL`, `OBJECT_STORE_S3_ACCESS_KEY_ID`, `OBJECT_STORE_S3_SECRET_ACCESS_KEY` | discovery, parser |
| `r2-backups` | `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (a token for the backups bucket only) | postgres-backup |
| `sec-edgar` | `SEC_CONTACT_EMAIL` (personal, so not in the public config) | discovery, load-companies |
| `gemini` (optional) | `GEMINI_API_KEY` | summarizer |
| `smtp` (optional until `EMAIL_BACKEND=smtp`) | `EMAIL_SMTP_USERNAME`, `EMAIL_SMTP_PASSWORD` | notifier |
| `cloudflared` | `TUNNEL_TOKEN` | cloudflared |
| `grafana-cloud` | `GRAFANA_CLOUD_OTLP_ENDPOINT`, `GRAFANA_CLOUD_INSTANCE_ID`, `GRAFANA_CLOUD_API_TOKEN` | otel-collector |

Not secrets, so in the config instead: `GOOGLE_CLIENT_ID` (public; the website embeds it too),
bucket names, `EMAIL_SMTP_HOST`, `EMAIL_SENDER`, and the website's origin (`API_CORS_ORIGINS`).
`secrets-example/` has a placeholder manifest for each Secret above, with notes on where each
value comes from; `tests/test_k8s_manifests.py` checks that every Secret a workload reads has one.

### Sealing a secret

Once, after the cluster exists, fetch the controller's public certificate. It is public, so commit
it: anyone can then seal secrets without access to the cluster.

```
kubeseal --fetch-cert > deploy/k8s/overlays/production/sealed-secrets/cert.pem
```

For each Secret:

```
mkdir -p deploy/k8s/secrets-local
cp deploy/k8s/secrets-example/postgres.yaml deploy/k8s/secrets-local/
$EDITOR deploy/k8s/secrets-local/postgres.yaml        # real values; this directory is gitignored
kubeseal --cert deploy/k8s/overlays/production/sealed-secrets/cert.pem --format yaml \
  < deploy/k8s/secrets-local/postgres.yaml \
  > deploy/k8s/overlays/production/sealed-secrets/postgres.yaml
```

Then list the file under `resources` in `overlays/production/kustomization.yaml`, run
`make k8s-check`, commit, and push. Argo CD applies the SealedSecret and the controller creates
the Secret. A SealedSecret only decrypts under its own name and namespace (kubeseal's default
"strict" scope), so a copy renamed or moved elsewhere is useless. Delete the plain copy in
`secrets-local/` when done, or keep it in a password manager rather than on disk.

- **Changing a value:** seal the whole Secret again and commit. Pods read Secrets as environment
  variables at startup, so restart the ones that use it:
  `kubectl -n filings rollout restart deployment/<name>`.
- **Back up the sealing key.** The SealedSecrets in git can only be decrypted with the
  controller's private key. A rebuilt cluster generates a new one, and then every secret must be
  sealed again. To avoid that, export the key once and keep it outside git, in a password manager
  (`*sealed-secrets-key*.yaml` is gitignored as a safety net):
  `kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key.yaml`.
  After a rebuild, restore it before sealing anything new:
  `kubectl apply -f sealed-secrets-key.yaml`, then
  `kubectl -n kube-system rollout restart deployment/sealed-secrets-controller`. The controller
  adds a new key every 30 days and keeps the old ones, so re-export now and then.

## First install (Phase 7)

The VM's first-boot bootstrap (`infra/terraform/modules/oci/bootstrap.sh`, ADR-0018) does the
first three steps; the rest are by hand. The service images must exist by then: CI has to have
published and deployed them once ([Deploying](#deploying)), or the services can't start.

1. **k3s**, pinned, without the bundled Traefik ingress controller and service load balancer
   (Cloudflare Tunnel replaces both, and they cost RAM), with local-path volumes on the block
   volume.
2. **Argo CD:** `kubectl apply --server-side --force-conflicts -k deploy/argocd/install`
3. **The applications:** `kubectl apply -k deploy/argocd`. Argo CD installs the Sealed Secrets
   controller, and syncs the application in waves: the namespace, the config and Secrets,
   Postgres, the migrations, then everything else.
4. **Secrets:** on a first install there are none yet, so the application's first sync waits
   for a Postgres that can't start without its Secret. Fetch the certificate, seal each secret
   (above), commit, and push. Argo CD picks them up by itself: a sync still waiting after
   30 minutes fails, and the retry, or the next automated sync, starts from the latest commit.
   To skip the wait, terminate the waiting sync in the Argo CD UI.
   After a rebuild, restore the sealing key instead (above): the SealedSecrets are already in the
   repository, and the application's syncs fail and retry until the controller is up.
5. **Companies:** discovery only polls companies flagged from the watchlist, so load them once
   instead of waiting for the daily run:
   `kubectl -n filings create job --from=cronjob/load-companies load-companies-initial`
6. **Back up the sealing key** (above).

Argo CD's web UI is not exposed. To look at it:
`kubectl -n argocd port-forward svc/argocd-server 8080:443`, then sign in as `admin` with the
password from `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d`.

## Deploying

A push to `main` that changes the services deploys itself
([ADR-0019](../../docs/adr/0019-image-ci-and-deploy.md)):

1. `.github/workflows/images.yml` builds the five images natively for amd64 and arm64 and
   publishes them to GHCR, tagged `sha-<short commit SHA>` and `main`. It runs when `services/`,
   `libs/`, `docker/`, `pyproject.toml`, `uv.lock`, `.dockerignore`, or
   `config/watchlist.toml` change.
2. Once CI has passed on the same commit, it sets that tag in
   `overlays/production/images/kustomization.yaml` and commits it to `main` as
   `github-actions[bot]`: "Deploy sha-... to production [skip ci]".
3. Argo CD sees the commit within 3 minutes and rolls the services out.

`main` on GitHub is then a commit ahead of your checkout: pull before pushing again.

- **Not active yet:** the workflow's jobs run only while the repository is public, because
  that is when its arm64 runners and GHCR storage are free. Until the first deploy, the tags are
  a placeholder (`0.1.0`) that doesn't exist in GHCR.
- **After the first run,** make each of the five GHCR packages public (the package's
  **Package settings → Change visibility**), if it isn't already.
- **Deploy by hand:** **Actions → Images → Run workflow** on `main`. If a deploy job failed
  because CI did, re-run CI on that commit, then the failed deploy job.
- **Roll back:** revert the bot's deploy commit (`git revert <commit>`), or set an older `sha-`
  tag in `overlays/production/images/kustomization.yaml`, and push. The next build of `main`
  deploys normally again.
- **Check a published image:**
  `docker buildx imagetools inspect ghcr.io/asood8/filings-api:sha-<short commit SHA>` lists
  both architectures, plus the build's provenance and SBOM attestations.

## One-off commands

The pipeline's one-off commands (ADR-0014) run inside a pod that already has the settings,
Secrets, and network access they need. The NetworkPolicies admit pods by name, so a pod started
some other way can't reach the data stores.

```
# Re-publish events, e.g. to re-summarize 8-Ks after a model or prompt change (ADR-0007).
kubectl -n filings exec deploy/summarizer -- filings-replay parsed --form-type 8-K
kubectl -n filings exec deploy/summarizer -- filings-replay discovered --status discovered
# Rebuild the search index and embed what's missing (ADR-0011).
kubectl -n filings exec deploy/summarizer -- filings-index
# Run discovery now instead of waiting for the schedule.
kubectl -n filings create job --from=cronjob/discovery discovery-now
# Is a worker's loop alive? Its liveness probe runs this (ADR-0017); it prints why not.
kubectl -n filings exec deploy/parser -- filings-healthcheck filings-parser
# Download missing 8-K exhibits: a discovery job with a different command.
kubectl -n filings create job --from=cronjob/discovery fetch-exhibits --dry-run=client -o json \
  | jq '.spec.template.spec.containers[0].args = ["filings-discovery", "fetch-exhibits"]' \
  | kubectl create -f -
```

Every service image includes `filings-replay` and `filings-index`. The summarizer's pod is the
one to use: it can reach Postgres, MongoDB, and the embeddings server. Replays only write outbox
rows, which the outbox relay then publishes.

## Upgrading pinned components

Every image and upstream manifest is pinned to a version:

- **Our services:** CI sets their tags in `overlays/production/images/kustomization.yaml` on
  every deploy ([Deploying](#deploying)).
- **kustomize for the deploy job:** `KUSTOMIZE_VERSION` and its checksum in
  `.github/workflows/images.yml`. It must be the version inside the pinned kubectl
  (`kubectl version --client`), which `make k8s-check` renders with; a test checks.
- **Data stores and tools:** the image tags in `base/` (Postgres and Valkey match
  `docker-compose.yml`; keep them in step).
- **llama.cpp and the models:** the image tag in `base/llm.yaml` and `base/embeddings.yaml`; the
  model URLs are pinned to a Hugging Face commit and checked against a SHA-256.
- **Sealed Secrets:** replace `platform/sealed-secrets/controller.yaml` with the new release's,
  and update its checksum in `scripts/check-k8s.sh` and `KUBESEAL_VERSION` in
  `scripts/install-k8s-tools.sh`.
- **Argo CD:** the version in `deploy/argocd/install/kustomization.yaml`'s URL; apply it again.
- **Validation tools:** the versions and checksums in `scripts/install-k8s-tools.sh`, and the
  Kubernetes version in `scripts/check-k8s.sh` (k3s's stable channel).
