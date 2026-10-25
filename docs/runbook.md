# Runbook

What to do when something happens to the production system: one k3s node on Oracle Cloud
(ADR-0017), the cloud resources around it (Terraform, ADR-0018), container images from GHCR
(ADR-0014), and telemetry in Grafana Cloud (ADR-0016). One section per situation; commands are
meant to be copied.

**The cluster and the accounts don't exist yet** (Phase 7). The commands follow the manifests in
`deploy/`; the `rpk` commands and the backup drill were tried against the local stack. The first
time something here turns out different in production, fix this file.

Background: [deploy/k8s/README.md](../deploy/k8s/README.md) (install, sealing secrets, one-off
commands, upgrades) and [infra/terraform/README.md](../infra/terraform/README.md) (accounts,
applying, reaching the cluster, Terraform state).

- [Before you start](#before-you-start)
- [Alert: No new filings in 24 hours](#alert-no-new-filings-in-24-hours)
- [Alert: Dead-letter topic not empty](#alert-dead-letter-topic-not-empty)
- [Alert: Consumer lag high](#alert-consumer-lag-high)
- [Alert: Pipeline service not reporting](#alert-pipeline-service-not-reporting)
- [Alert: R2 free tier half used](#alert-r2-free-tier-half-used)
- [Alert: R2 usage not measured](#alert-r2-usage-not-measured)
- [Dead letters: inspect, fix, replay, trim](#dead-letters-inspect-fix-replay-trim)
- [Deploys and rollbacks](#deploys-and-rollbacks)
- [Pausing Argo CD for hands-on work](#pausing-argo-cd-for-hands-on-work)
- [Rotating secrets](#rotating-secrets)
- [Dependency audit fails](#dependency-audit-fails)
- [SEC 429s and fair access](#sec-429s-and-fair-access)
- [Swapping the local LLM model](#swapping-the-local-llm-model)
- [Changing search ranking or the embedding model](#changing-search-ranking-or-the-embedding-model)
- [Oracle idle reclamation and capacity errors](#oracle-idle-reclamation-and-capacity-errors)
- [Backups and restores](#backups-and-restores)
- [Capacity planning](#capacity-planning)
- [Enforce the website's Content-Security-Policy](#enforce-the-websites-content-security-policy)

## Before you start

Open the SSH tunnel to the Kubernetes API (infra/terraform/README.md, "Reaching the cluster").
Everything below is `kubectl` from your machine; the namespace is `filings`.

```
kubectl -n filings get pods,cronjobs                 # what runs, restarts, last schedules
kubectl -n argocd get applications                   # SYNC STATUS and HEALTH of filings, sealed-secrets
kubectl top pods -n filings                          # CPU and memory now (k3s ships metrics-server)
kubectl -n filings logs deploy/summarizer --since=1h
kubectl -n filings logs deploy/parser --previous     # the last run of a crashed container
kubectl -n filings exec redpanda-0 -- rpk group describe parser summarizer notifier
kubectl -n filings exec -it postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

- **Pipeline commands** (`filings-replay`, `filings-index`, `filings-benchmark`) run in the
  summarizer's pod, which can reach Postgres, MongoDB, the LLM, and the embeddings server:
  `kubectl -n filings exec deploy/summarizer -- <command>`. Pods started any other way are shut
  out by the NetworkPolicies. A command shares the container's 256 Mi memory limit with the
  running summarizer, so run one at a time. `filings-rebuild-key-fields` ships with the parser
  instead: `kubectl -n filings exec deploy/parser -- filings-rebuild-key-fields --form-type 4`
  rewrites Form 4 key fields and transaction lines from MongoDB, for example after a migration
  adds a key-field table (it calls nothing at SEC and publishes nothing).
- **Logs** stay in the cluster (only traces and metrics go to Grafana Cloud), and only the current
  and previous run of each container are kept. Lines logged while a filing or a request is being
  handled end with `trace_id=… span_id=…`; look the trace up in Grafana Cloud's Tempo.
- **Dashboards:** "SEC Filings: Pipeline" and "SEC Filings: API", in the "SEC Filings" folder.
- **Locally**, the same tools: `docker compose exec redpanda rpk ...`, `uv run filings-replay ...`.

## Alert: No new filings in 24 hours

Warning. Nothing was discovered (`stage=discovered`), or nothing reached a summary
(`stage=summarized`), in the last 24 hours. It is only checked from Tuesday 12:00 to Saturday
12:00 UTC, so every window covers SEC filing hours.

1. **A US federal holiday** (or the day after one) fires it: EDGAR takes no filings. Silence the
   alert in Grafana until the next business day.
2. **`discovered`:** discovery runs every 10 minutes as a CronJob.

   ```
   kubectl -n filings get cronjob discovery           # LAST SCHEDULE under 10 minutes ago, SUSPEND False
   kubectl -n filings get jobs --sort-by=.status.startTime | tail -3
   kubectl -n filings logs job/<newest discovery job>
   ```

   - SEC 403 or 429 errors: [SEC 429s and fair access](#sec-429s-and-fair-access).
   - A settings error about `SEC_CONTACT_EMAIL`: the `sec-edgar` Secret is missing or empty
     ([Rotating secrets](#rotating-secrets)).
   - Valkey connection errors: the shared SEC rate limiter lives there, and discovery stops rather
     than call SEC unthrottled. Check the valkey pod.
   - It runs but polls no companies: load the watchlist,
     `kubectl -n filings create job --from=cronjob/load-companies load-companies-now`.
3. **`summarized` only:** filings are found but stop after that. In order:
   - The outbox relay: `kubectl -n filings logs deploy/outbox-relay --since=1h`. Unpublished
     events (in psql): `SELECT count(*) FROM outbox WHERE published_at IS NULL;`
   - Consumer lag: `kubectl -n filings exec redpanda-0 -- rpk group describe parser summarizer`.
   - Dead letters: [Dead letters](#dead-letters-inspect-fix-replay-trim).

Form 4s are summarized without the LLM and are most filings, so an LLM outage alone rarely fires
this rule; the lag and dead-letter rules catch it.

## Alert: Dead-letter topic not empty

Warning, fires at once. A stage gave up on a message (its retries ran out, or the error was
permanent) and moved it to `<topic>.dlq`. Nothing reads those topics, so the alert keeps firing
until the topic is trimmed. Follow [Dead letters](#dead-letters-inspect-fix-replay-trim).

## Alert: Consumer lag high

Warning. A consumer group has been more than 100 messages behind on a topic for 15 minutes.

```
kubectl -n filings exec redpanda-0 -- rpk group describe <group>   # LAG per partition, members
```

- **A replay is running.** A replay of 8-Ks goes through the local LLM at minutes per filing (up
  to about 5 on two laptop cores), so 100 8-Ks take hours. Expected: let it finish, or silence
  the alert.
- **summarizer:** look for failovers and circuit-breaker messages in its log. Then the llm pod:
  `kubectl -n filings describe pod -l app.kubernetes.io/name=llm` (restarts, `OOMKilled`). If the
  local model is down and Gemini answers 429 (free daily quota used up), the backlog waits for one
  of them; nothing is lost. An llm pod killed every few 8-Ks, with memory rising after each one,
  is keeping past prompts in RAM: check its arguments still include `--cache-ram 0` (ADR-0017).
- **parser, notifier:** repeated retries in the log point at a dependency: MongoDB Atlas for the
  parser (see the IP note under [Oracle](#oracle-idle-reclamation-and-capacity-errors)), webhook
  or SMTP errors for the notifier.
- **A stuck consumer:** the relay and the consumers keep a heartbeat, and their liveness probe
  (`filings-healthcheck <process>`) restarts one whose loop stops beating (ADR-0017). A process
  that is alive but making no progress can still be restarted by hand, which is always safe
  because redelivered messages are handled idempotently:
  `kubectl -n filings rollout restart deployment/<name>`.
  To see what the probe sees: `kubectl -n filings exec deploy/<name> -- filings-healthcheck <process>`.

## Alert: Pipeline service not reporting

Critical, fires at once. A long-running service (`filings-outbox-relay`, `-parser`,
`-summarizer`, `-notifier`, `-api`) sent metrics in the last day but not in the last 10 minutes.

- **All of them at once:** the telemetry path, not the services. Check the collector:
  `kubectl -n filings logs deploy/otel-collector --since=30m`. A 401 or 403 means the Grafana
  Cloud token was revoked or expired ([Rotating secrets](#rotating-secrets)).
- **One of them** (the deployment is the name without `filings-`):

  ```
  kubectl -n filings get pods -l app.kubernetes.io/name=<deployment>
  kubectl -n filings describe pod -l app.kubernetes.io/name=<deployment>   # Events at the bottom
  kubectl -n filings logs deploy/<deployment> --previous
  ```

  - `OOMKilled`: it outgrew its memory limit. The limits add up to the 9 GiB budget
    (ADR-0017, enforced by `tests/test_k8s_manifests.py`), so raising one means lowering another.
  - `ImagePullBackOff`: the tag in `deploy/k8s/overlays/production/images/kustomization.yaml`
    on the `production` branch doesn't exist in GHCR (or is `main`'s placeholder, after a plain
    commit was pushed there by hand), or the GHCR package isn't public.
  - `CrashLoopBackOff` right after start: usually a missing Secret or setting; the log says which.
- **Stopped on purpose:** it fires for up to a day. Silence it.

## Alert: R2 free tier half used

Warning. The daily `r2-usage` job found over half of a monthly R2 free allowance used: storage
(10 GB), Class A operations (1 million writes and lists in 30 days), or Class B (10 million
reads). R2 bills past the allowance and has no cap, so act before 100%.

```
kubectl -n filings logs job/$(kubectl -n filings get jobs -l app.kubernetes.io/name=r2-usage \
  -o jsonpath='{.items[-1:].metadata.name}')     # each allowance's numbers
```

- **Storage:** usually backups. Their bucket expires them after `backup_retention_days`
  (Terraform, 30 by default): lower it and apply. Raw filings grow with the watchlist, a few MB
  a month at 35 companies.
- **Class A** (writes): normal use is a few thousand a month (one write per new filing and
  exhibit, one per nightly backup). Far more means something writes in a loop: check the
  discovery and backup jobs' logs and recent replays. Lists count too.
- **Class B** (reads): the parser reads each raw filing once; a replay of `discovered` reads
  them all again. Repeated replays add up.
- **An unknown operation type** in the log was counted as Class A, the safe side; if it is free
  in Cloudflare's price list, add it to `FREE` in `filings_common/r2_usage.py`.

## Alert: R2 usage not measured

Warning. The `r2-usage` job reported in the last week but not in the last 50 hours, so the alert
above can't fire. `kubectl -n filings get cronjob r2-usage` (last schedule) and the latest job's
log, as above. A 400 or 403 from Cloudflare's analytics API means the `cloudflare-analytics`
token was revoked or lost its Account Analytics: Read permission ([Rotating
secrets](#rotating-secrets)).

## Dead letters: inspect, fix, replay, trim

| Dead-letter topic | Stage that gave up | How to replay |
|---|---|---|
| `filings.discovered.dlq` | parser | `filings-replay discovered --accession <no>` |
| `filings.parsed.dlq` | summarizer | `filings-replay parsed --accession <no>` |
| `filings.ready.dlq` | notifier | Re-publish the dead letters themselves (step 4) |

1. **See what is there.** Each message's key is the filing's accession number. Note each
   partition's HIGH-WATERMARK for step 6.

   ```
   kubectl -n filings exec redpanda-0 -- rpk topic describe filings.parsed.dlq -p
   kubectl -n filings exec redpanda-0 -- rpk topic consume filings.parsed.dlq --offset :end \
     --format '%p/%o %k %h{%k=%v  }\n'
   ```

   The headers say why: `dlq.error` (the exception, first 1,000 characters), `dlq.attempts`,
   `dlq.source` (original topic/partition/offset), and `traceparent`, whose second field is the
   trace ID. `--offset :end` reads what is there now and stops.
2. **Find the cause.** The consumer logged `dead-lettering <topic>@<offset> after <n> attempt(s)`
   with the error, and the trace in Tempo shows every attempt:
   `kubectl -n filings logs deploy/summarizer --since=24h | grep -A 3 dead-lettering`.
3. **Fix it:** deploy a code fix ([Deploys](#deploys-and-rollbacks)), correct the data, or, if it
   was an outage that has ended (LLM, Atlas, a webhook), nothing.
4. **Replay.** Replays write outbox rows, which the relay publishes; every consumer is idempotent,
   so a replay never duplicates a filing or an alert.

   ```
   kubectl -n filings exec deploy/summarizer -- filings-replay parsed \
     --accession 0000320193-26-000101 --accession 0000320193-26-000102
   ```

   `--form-type 8-K` replays a whole form type, and `--status` (discovered replays only) a status.
   **`filings.ready.dlq`** can't be replayed from Postgres: the summarizer announces a filing as
   ready only once. Re-publish the dead letters instead; the notifier sends only alerts that are
   still pending (ADR-0012):

   ```
   kubectl -n filings exec redpanda-0 -- rpk topic consume filings.ready.dlq --offset :end \
     --format '%k %v\n' > ready-dlq.txt
   # keep only the lines to deliver again ("<accession no> <event JSON>"), then:
   kubectl -n filings exec -i redpanda-0 -- rpk topic produce filings.ready --format '%k %v\n' \
     < ready-dlq.txt
   rm ready-dlq.txt
   ```

   They start new traces (their trace headers aren't copied). Alerts marked `failed` (a deleted
   webhook, a refused address) are permanent and are not sent again.
5. **Check.** In psql: `SELECT accession_no, status FROM filings WHERE accession_no IN ('...');`
   shows `ready` or `notified` once through. `rpk topic describe` again: no new dead letters.
6. **Trim** the topic, so the alert resolves (within a few minutes):

   ```
   kubectl -n filings exec redpanda-0 -- rpk topic describe filings.parsed.dlq -p   # same as in step 1?
   kubectl -n filings exec redpanda-0 -- rpk topic trim-prefix filings.parsed.dlq --offset end --no-confirm
   ```

   If a dead letter arrived after step 1, trim only what you handled, partition by partition, to
   the high watermarks you noted: `rpk topic trim-prefix filings.parsed.dlq -p 2 -o 5 --no-confirm`.

## Deploys and rollbacks

Argo CD keeps the cluster equal to `deploy/k8s/overlays/production` on the **`production`
branch**, polling every 3 minutes. Only the image workflow (`.github/workflows/images.yml`,
ADR-0019) moves that branch: for each push to `main` that changes the code or anything under
`deploy/`, once that commit's images are in GHCR and CI has passed on it, it pushes a **deploy
commit** (that commit of `main` with the image tags set) to `production`. A push to `main`
therefore goes live 5 to 20 minutes later, never before its images. Each sync runs the migrations
Job first (wave -1), then rolls out the rest.

```
kubectl -n argocd get application filings -w                                      # SYNC STATUS, HEALTH
kubectl -n argocd get application filings -o jsonpath='{.status.sync.revision}{"\n"}'   # the deploy commit it runs
kubectl -n argocd get application filings -o jsonpath='{.status.operationState.message}{"\n"}'
kubectl -n filings logs job/migrate                                               # last migration run
kubectl -n argocd annotate application filings argocd.argoproj.io/refresh=normal --overwrite   # check git now
git fetch origin && git log -1 origin/production   # "Source commit <SHA>": the commit of main it runs
```

The web UI shows the same with more detail: `kubectl -n argocd port-forward svc/argocd-server
8080:443` (deploy/k8s/README.md has the sign-in).

**A push that didn't deploy:** open its run under **Actions → Images**. CI failed or was cancelled
on that commit (the deploy job says so: re-run CI, then the deploy job); "Production already
deploys ..." (a newer commit is already out); or no run at all (the push changed nothing the
workflow watches, such as docs only). If the application reports that it can't find the
`production` branch, no deploy has run yet (a new repository or fork): run the workflow by hand
(**Actions → Images → Run workflow** on `main`), which creates it; don't push `main` there, since
its image tags are a placeholder.

**Roll back** one of two ways, then fix `main`:

1. **Deploy an older commit** (the usual way). **Actions → Images → Run workflow**, branch
   `main`, with `commit` set to the commit of `main` to go back to (a `Source commit` from an
   earlier deploy, or one from `git log`) and **rollback** ticked; or, with the GitHub CLI,
   `gh workflow run images.yml --ref main -f commit=<SHA> -f rollback=true`. Its images are
   normally still in GHCR, so nothing is built: the run checks CI's result on that commit (it must
   have passed) and pushes the deploy commit, in a few minutes. Let a queued Images run finish
   first: a run started by hand and a push's waiting run replace each other.
2. **Push an earlier deploy commit back** (no Actions involved; Argo CD picks it up within
   3 minutes):

   ```
   git fetch origin
   git reflog show origin/production    # deploy commits this clone has fetched, newest first
   git push --force origin <deploy commit>:refs/heads/production
   ```

   A deploy commit's SHA is also in the Images run that made it (its last notice, "deploy commit
   <SHA>"), and in Argo CD's history:
   `kubectl -n argocd get application filings -o jsonpath='{range .status.history[*]}{.revision}{"  "}{.deployedAt}{"\n"}{end}'`.
   If your clone doesn't have it, `git fetch origin <full SHA>` gets it while GitHub still keeps
   it. Push only deploy commits (their subject starts with "Deploy"): a plain commit of `main` has
   placeholder image tags, and nothing could start.

**Then fix `main`:** revert the change (`git revert <commit>`, several:
`git revert <oldest>^..<newest>`; `git push`) or fix it forward. Until then, the next push that
the workflow deploys brings the bad change back with it, and so does re-running the bad commit's
Images run. A revert on its own also rolls back, but if it changes code it waits for a full image
build (10 to 20 minutes).

Not `kubectl rollout undo`, `kubectl edit`, or `kubectl set image`: self-heal undoes them within
minutes. Argo CD's own History and Rollback refuses to run while automated sync is on, and would
leave git and the cluster disagreeing anyway.

**When the commit you roll back from added a migration,** the older code doesn't know the
database's revision, so after the rollback the migrations Job fails ("Can't locate revision") and
blocks the sync. Fix forward instead, or downgrade the schema first, while the new image still
runs, then roll back. A downgrade can drop data (a removed column), so
[run a backup](#backups-and-restores) first:

```
kubectl -n filings exec deploy/summarizer -- python -c \
  'from alembic import command; from filings_common.db import alembic_config as c; command.current(c())'
kubectl -n filings exec deploy/summarizer -- python -c \
  'from alembic import command; from filings_common.db import alembic_config as c; command.downgrade(c(), "-1")'
```

## Pausing Argo CD for hands-on work

Self-heal undoes manual changes to anything git sets, such as replica counts. To stop parts of the
system for a while (a restore, a long investigation), pause automated sync first:

```
kubectl -n argocd patch application filings --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
```

Stop every writer, and the CronJobs that write:

```
kubectl -n filings scale deployment api outbox-relay parser summarizer notifier --replicas=0
kubectl -n filings patch cronjob discovery -p '{"spec":{"suspend":true}}'
kubectl -n filings patch cronjob load-companies -p '{"spec":{"suspend":true}}'
```

To resume, from a checkout of `main`, re-apply the Applications (they come from git, not from
Argo CD itself); Argo CD restores the replica counts. Git doesn't set `suspend`, so resume the
CronJobs yourself:

```
kubectl apply -k deploy/argocd
kubectl -n filings patch cronjob discovery -p '{"spec":{"suspend":false}}'
kubectl -n filings patch cronjob load-companies -p '{"spec":{"suspend":false}}'
```

## Rotating secrets

The general procedure, for any Secret:

1. **Make the new credential** at the provider, keeping the old one valid for now.
2. **Seal the whole Secret again** (deploy/k8s/README.md, "Sealing a secret"). For a Secret with
   one key, without a plain copy on disk or in your shell history:

   ```
   read -rs VALUE        # paste the value, then Enter
   printf %s "$VALUE" \
     | kubectl create secret generic gemini -n filings --dry-run=client -o yaml \
         --from-file=GEMINI_API_KEY=/dev/stdin \
     | kubeseal --cert deploy/k8s/overlays/production/sealed-secrets/cert.pem --format yaml \
     > deploy/k8s/overlays/production/sealed-secrets/gemini.yaml
   unset VALUE
   ```

3. **`make k8s-check`, commit, push.** Once CI passes on it, the image workflow deploys it to
   `production` ([Deploys](#deploys-and-rollbacks)), Argo CD applies the SealedSecret, and the
   controller updates the Secret.
4. **Restart what reads it,** once Argo CD has synced that deploy: pods read Secrets at startup.
   `kubectl -n filings rollout restart deployment/<name>`. CronJobs use the new value on their
   next run.
5. **Check it works, then revoke the old credential.**
6. **Local development** has its own copies in `.env`. Change them there too if they are the same
   credential, and never print `.env` lines (check a value with code that prints only whether it
   is set, or its length).

| Secret: keys | New value from | Restart | Notes |
|---|---|---|---|
| `gemini`: `GEMINI_API_KEY` | Google AI Studio, API keys | summarizer | Delete the old key in AI Studio afterwards. |
| `sec-edgar`: `SEC_CONTACT_EMAIL` | Your address (personal rather than secret) | none: discovery and load-companies are CronJobs | SEC sees it in every request's User-Agent. |
| `smtp`: `EMAIL_SMTP_USERNAME`, `EMAIL_SMTP_PASSWORD` | OCI console: your user, SMTP credentials (a user can have two at once) | notifier | Delete the old SMTP credential. |
| `r2-raw-filings`: endpoint, key ID, secret | Cloudflare R2, Manage API tokens: Object Read & Write on `filings-raw` only | parser (discovery: next run) | Delete the old token. |
| `r2-backups`: endpoint, key ID, secret | The same, on `filings-backups` only | none: a CronJob | [Run a backup now](#backups-and-restores) to prove it, then delete the old token. |
| `cloudflared`: `TUNNEL_TOKEN` | Cloudflare Zero Trust, Networks, Tunnels: refresh the tunnel's token; then `terraform apply -refresh-only` and pipe `terraform output -raw tunnel_token` into kubeseal (infra/terraform/README.md) | cloudflared | Connectors that start with the old token are refused, so roll out right away; check `https://api.<domain>/healthz`. |
| `grafana-cloud`: endpoint, instance ID, `GRAFANA_CLOUD_API_TOKEN` | Grafana Cloud portal, Access Policies: a new token on the collector's policy | otel-collector | Delete the old token; "Pipeline service not reporting" clears within minutes if it fired. |
| `cloudflare-analytics`: `CLOUDFLARE_ANALYTICS_TOKEN` | Cloudflare, My Profile, API Tokens: a new token with only Account Analytics: Read | r2-usage | Delete the old token; the next daily run uses the new one (or start one: `kubectl -n filings create job --from=cronjob/r2-usage r2-usage-now`). |
| `postgres`: `POSTGRES_USER`, `POSTGRES_PASSWORD` | `openssl rand -hex 32` (URL-safe) | every Deployment (below) | The Secret sets the password only when the database is created, so change it in Postgres too (below). |
| `mongodb`: `MONGODB_URL` | `terraform apply -replace=module.atlas.random_password.database_user`, then pipe `terraform output -raw mongodb_url` into kubeseal | parser, summarizer | The old password stops working at the apply: seal and push right after. |

Not in the cluster, rotated where they live: Terraform's own credentials (`CLOUDFLARE_API_TOKEN`,
`MONGODB_ATLAS_CLIENT_ID`/`_SECRET`, `GRAFANA_AUTH`, the OCI API key in `~/.oci`), and, if you
added a Discord contact point in Grafana Cloud (alerts otherwise go to the stack's default email
contact point; ADR-0018), its webhook: make a new webhook in Discord, update the contact point,
delete the old webhook.

**`GOOGLE_CLIENT_ID` is configuration, not a secret** (the website embeds it). To change it, set it
in `deploy/k8s/overlays/production/config.env` (the API restarts by itself: the ConfigMap's name
changes) and in Terraform's `google_client_id` (the website's build on Pages), and push and apply
together. Sessions already issued stay valid.

**The Postgres password,** in order: change it in the database, seal and push the new Secret, then
restart. Between the first and last step new connections fail, so do it in one go:

```
read -rs NEW_PASSWORD
kubectl -n filings exec -i postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1' <<EOF
ALTER ROLE CURRENT_USER PASSWORD '$NEW_PASSWORD';
EOF
# seal and push the postgres Secret with POSTGRES_PASSWORD=$NEW_PASSWORD, wait for the sync, then:
kubectl -n filings rollout restart deployment/api deployment/outbox-relay deployment/parser \
  deployment/summarizer deployment/notifier
unset NEW_PASSWORD
```

## Dependency audit fails

The "Dependency audit" workflow (`.github/workflows/audit.yml`, on every push to `main`, every
pull request, and weekly on Mondays; locally `make audit`, which the workflow runs) checks every
package version in `uv.lock` with pip-audit (PyPI's advisory data) and the website's runtime
packages with `npm audit` (high and critical only). A new advisory fails it even when nothing in
the repository changed. It is separate from CI, so a failure doesn't block deploys.

1. **See what it found:** `make audit` locally names the package, the advisory, and the fixed
   version.
2. **Python:** upgrade the package in the lock file, then check and commit `uv.lock`. For a
   package that only comes in through another one, the fix may need that one upgraded instead.

   ```
   uv lock --upgrade-package <name>
   uv sync && make lint test && make audit
   ```

   The fix reaches production with the next image tag ([Deploys](#deploys-and-rollbacks)).
3. **Website:** `npm --prefix web update <name>` (or `npm --prefix web audit fix`), then
   `make web-check`, and commit `web/package-lock.json`. A nested dependency whose parent hasn't
   caught up can be pinned to a patched version with `overrides` in `web/package.json`. The fix
   reaches users with the next website build.
4. **No fix yet, or it can't affect this project** (the vulnerable feature isn't used): say why
   in the commit and skip that one advisory with `--ignore-vuln <ID>` on the pip-audit command in
   the `Makefile`; remove it once a fix is out. `npm audit` can't skip a single advisory: replace
   or drop the package.

## Updating a GitHub Action

Every `uses:` in `.github/workflows/` names a commit SHA, with the version it was in a comment
([ADR-0019](adr/0019-image-ci-and-deploy.md#consequences)): a tag can be moved to other code, a
SHA can't. To move to a newer version, read its release notes, then:

```
gh api repos/<owner>/<action>/commits/<tag> --jq .sha   # e.g. actions/checkout, v7.0.2
```

Replace the SHA in every workflow that uses the action, update the version comment, and open a
pull request: CI runs the changed workflows, and `actionlint` (pre-commit) checks their syntax.

## SEC 429s and fair access

SEC's rules for automated access: **at most 10 requests per second**, and a **User-Agent naming
the application and a contact email**. Going over brings 429s, then a block of the address (a 403
page about the request rate) that lifts after about 10 minutes under the limit.

How the pipeline keeps to them (ADR-0004, `filings_common.edgar`):

- Every SEC request goes through `EdgarClient`, with the User-Agent
  `sec-filings-pipeline <SEC_CONTACT_EMAIL>`.
- One token bucket in Valkey (key `ratelimit:sec`) is shared by every process: 5 requests/s,
  burst 1 (`SEC_RATE_LIMIT_PER_SECOND`, `SEC_RATE_LIMIT_BURST`; the settings refuse more than
  10 and 5). If Valkey is down, discovery fails instead of going unthrottled.
- 429, 500, 502, 503, and 504 answers and connection errors are retried with exponential backoff,
  honoring `Retry-After`, up to 5 attempts (`SEC_MAX_ATTEMPTS`). A 403 is not retried.
- The load: one submissions request per watchlist company per run (35 every 10 minutes, about
  7 s at 5 per second), plus each new filing's documents.

When 429s show up (the "SEC 429s" and "SEC responses per hour" panels on the Pipeline dashboard,
or `SEC returned 429` in discovery's log):

1. **Look for another client.** Anything that calls SEC outside `EdgarClient` and the cluster's
   Valkey (a script, a second deployment, a run pointed at another Redis) doesn't share the bucket.
2. **Slow down:** set `SEC_RATE_LIMIT_PER_SECOND=2` in `deploy/k8s/overlays/production/config.env`
   and push. Once the image workflow has deployed it ([Deploys](#deploys-and-rollbacks)), the
   next discovery run uses it.
3. **While blocked (403s), pause discovery,** so new runs don't extend the block:
   `kubectl -n filings patch cronjob discovery -p '{"spec":{"suspend":true}}'`. Self-heal leaves
   it alone, since git doesn't set `suspend` (check SUSPEND in `kubectl -n filings get cronjob`).
   Resume with `"suspend":false` after 10 minutes or more.
4. **Nothing is lost:** discovery reads each company's recent filings list and skips only accession
   numbers it already has, so filings from the pause are picked up by the next run, as long as
   they are within its 30-day lookback (`DISCOVERY_LOOKBACK_DAYS`; raise it for one run after a
   longer pause).

Never raise the limit above SEC's maximum or drop the contact email. If SEC writes to the contact
address, answer.

## Swapping the local LLM model

The summarizer's local model is llama.cpp's server in the `llm` Deployment (ADR-0017); Gemini is
the fallback. `filings-benchmark` runs the collected 8-Ks through models with the production
prompt and schema, and reports per model the median and p95 time, failures (errors or answers that
don't fit the schema), fact recall and summaries with forbidden claims (scored against the labeled
8-Ks in `config/summary_eval.json`, ADR-0008), and how many summaries contain numbers the filing
doesn't. With `--out`, it saves every summary with what it missed and any forbidden claim, for
reading side by side; read the misses before trusting a difference of a filing or two.

The labels cover the 22 8-Ks in the local database (accession numbers), so the quality columns
only mean something there: production collects other 8-Ks, and on the VM they read `n/a`. Compare
quality locally, with the same model file if it is a different quantization (Ollama can load a
GGUF file through a Modelfile, `ollama create`), and speed on the VM.

1. **Compare quality locally first** (Ollama):

   ```
   make llm-up                                          # starts Ollama, pulls llama3.2:3b
   docker compose exec ollama ollama pull <candidate>
   uv run filings-benchmark --model llama3.2:3b --model <candidate> --out bench-local.json
   ```

   All 22 labeled 8-Ks take about 25 minutes per model on the laptop; `--accession <number>`
   (repeatable) runs chosen ones. `--gemini` adds Gemini as a reference (it uses the free daily
   quota); `--runs 3` repeats each. After a change to the labels, `--rescore bench-local.json`
   scores the saved summaries again without calling a model. A prompt change is measured the same
   way: run before and after, and keep it only if fact recall or forbidden claims improve without
   costing grounding, failures, or much time (bump `PROMPT_VERSION`, ADR-0007). Read the
   summaries behind an improvement too: an error reworded past its pattern scores as fixed. Add
   such errors to the labels and rescore both runs before deciding (ADR-0010).
2. **Point the `llm` Deployment at the new file** in `deploy/k8s/base/llm.yaml`: `MODEL_FILE`,
   `MODEL_URL` (pinned to a Hugging Face commit: `.../resolve/<commit>/<file>`), `MODEL_SHA256`
   (from the file's Hugging Face page), and the server's `--model` path and `--alias`. Set
   `LLM_MODEL` in `deploy/k8s/base/config.env` to the alias: it is stored with every summary.
   `make k8s-check`, commit, push. Once the image workflow has deployed it, Argo CD replaces the
   pod, and its init container downloads the model once into the 8 Gi volume. The old file stays
   there (room for three or four 2 GB models); it is under `/mnt/k3s-data/local-path/` on the VM.
3. **Measure on the VM,** whose cores are slower than a laptop's (ADR-0017 suggests trying a Q4_0
   file, which llama.cpp repacks for the A1's ARM cores):

   ```
   kubectl -n filings exec deploy/summarizer -- filings-benchmark --model <alias> --limit 10 --out /tmp/bench.json
   kubectl -n filings exec deploy/summarizer -- cat /tmp/bench.json > bench-<alias>.json
   kubectl top pod -n filings -l app.kubernetes.io/name=llm      # during the run: under the 3 Gi limit?
   ```

   llama.cpp's server answers with the one model it loaded, whatever `--model` says, so compare
   models one deployment at a time. The live summarizer shares the server's single slot: run the
   benchmark when the summarizer's lag is 0, or its times include waiting. A model that needs
   more memory needs the budget in ADR-0017 revisited.
4. **Keep it or revert the commit.** To re-summarize past 8-Ks with the new model:
   `kubectl -n filings exec deploy/summarizer -- filings-replay parsed --form-type 8-K` (minutes
   per filing; the lag alert may fire, as expected). Record the comparison in an ADR: the model
   choice is an open decision in the brief (§12).

Locally, `LLM_MODEL` in `.env` selects the Ollama model the pipeline uses.

## Changing search ranking or the embedding model

`filings-search-eval` measures search quality (ADR-0011). It runs the labeled queries in
`config/search_eval.json` through keyword, semantic, and hybrid search with the API's code, and
prints recall@5, recall@10, MRR, and how much of the top 5 Form 4s take. Run it locally before
and after changing fusion, the search documents, or the embedding model. Keep the change if the
numbers improve and no query gets much worse.

```
make up && make llm-up                      # Postgres, and Ollama with nomic-embed-text
uv run filings-search-eval --per-query --json data/eval-before.json
# make the change; then uv run filings-index if it changes the search documents (keywords,
# fields) or the embedding model (set EMBEDDING_MODEL first)
uv run filings-search-eval --per-query --json data/eval-after.json
```

- **Safe next to the running pipeline:** it only reads (read-only transactions). It waits for a
  busy embedding model instead of falling back to keywords the way the API does.
- **No embedding model:** without `EMBEDDING_MODEL`, or with the model down, it measures keyword
  search only and says why.
- **Other databases:** the labels fit the local database of the October 2026 watchlist run, and
  the tool lists any labeled filings it can't find. Against another database (production has
  other filings), relabel first; the label file's `description` says how the labels were made.
- **Vector size:** an embedding model with another vector size needs a migration first
  (ADR-0011).

## Oracle idle reclamation and capacity errors

**Idle reclamation.** Oracle may reclaim (stop) an Always Free instance whose CPU (95th
percentile), network, and memory use all stay under 20% for 7 days. While the workloads run, the
loaded models and the data stores should keep memory above 20% of 12 GB (their requests alone add
up to 4.5 GiB); the risk is a VM that is stopped, or whose workloads are down, for a week. At the
smaller size (1 OCPU, 6 GB; ADR-0020) there are no models, but the data stores still hold more
than 20% (Redpanda alone reserves 1 GiB).

- **Check:** the OCI console, Compute, Instances, the instance, Metrics; or `kubectl top node`.
  Memory utilization must have data there: Oracle sees memory only through the Oracle Cloud
  Agent's monitoring plugin, which Terraform enables. If the chart is empty after the first boot,
  the agent isn't running (`sudo snap services oracle-cloud-agent` on the VM), and the VM's
  safety margin is gone: CPU and network are usually under 20%.
- **Not a keep-alive job:** burning CPU to look busy would take the cores the LLM needs. Memory
  is the margin, and the "Pipeline service not reporting" alert says within minutes if the
  workloads that hold it stop, long before the 7 days.
- **Reclaimed or stopped:** start it from the console (or `oci compute instance action --action
  START --instance-id "$(terraform output -raw vm_instance_id)"` with the OCI CLI). k3s starts at
  boot and Argo CD resyncs; the boot and block volumes are kept.
- **If the public IP changed,** run `terraform apply`: it updates Atlas's IP access list. Until
  then the parser and summarizer can't reach MongoDB. (The tunnel doesn't depend on the IP.)
- **Atlas M0 pauses after 60 days without connections:** only if the cluster was down that long.
  Resume it in the Atlas UI.

**"Out of host capacity"** when creating or starting the instance: Oracle has no free A1 host in
that availability domain right now. The configuration is fine.

- Run `terraform apply` again later (everything else is already created), or for a new instance
  try another `oci_availability_domain_number` in regions that have several.
- Or let `scripts/retry-vm-capacity.sh` do both: every minute it plans only the instance
  (`-target`) in the next availability domain, checks the plan creates nothing else, and applies
  it, for up to 24 hours (`INTERVAL`, `MAX_HOURS`; throttling, by Oracle or by R2 where the
  state lives, waits `THROTTLE_WAIT` and tries again; so does the network failing while it
  plans, and it releases a plan lock it finds left on this machine by a plan that was cut off,
  with no terraform running). An attempt takes about 12 seconds and
  touches nothing else. Attempts alternate between the sizes in `SIZES` (default `"2:12 1:6"`,
  OCPU:GB, largest first): Oracle often has room for the smaller one sooner (ADR-0020). Once the
  instance exists it records the domain and size in `production.tfvars` and finishes the VM in
  two checked steps. First the still-empty data volume (moved to that domain if it was in
  another) and its attachment, which the VM's bootstrap waits up to 20 minutes for. Then Atlas's
  entry for its IP, which Atlas's API refuses while the laptop's IP isn't on the service
  account's API access list (a home IP changes now and then): the VM runs anyway, and the log
  says to add the IP and run `make tf-plan` and `make tf-apply`. Ashburn refused about 2,700
  attempts over three days in October 2026; the laptop must stay awake for it.
- **A VM with the smaller size grows in place.** The same script, started again or still
  running, asks Oracle for a compute capacity report every `GROW_INTERVAL` seconds (5 minutes)
  and resizes the VM to 2 OCPU and 12 GB only once Oracle answers `AVAILABLE`. A resize reboots
  the VM, so it is never retried blindly. Once it has grown, delete the `small-vm` line in
  `deploy/k8s/overlays/production/kustomization.yaml` in a pull request: the local LLM and
  semantic search come back with the next deploy.
- **Ask Oracle by hand** with the capacity report itself, from
  `infra/terraform/tools/capacity-report` (`terraform init`, then `terraform apply` with
  `-var` for `region`, `compartment_ocid`, `availability_domain`, `ocpus`, and `memory_gbs`; it
  creates nothing).
- Don't terminate or `-replace` a working VM casually: a new one needs capacity again.
- Upgrading the account to Pay As You Go gets priority for capacity, but it bills what exceeds
  the free allowance instead of refusing it. It is the user's decision, and only with the
  guardrails below in place.
- If capacity never comes, brief §11 lists the fallbacks.

## Guardrails against charges

The free Oracle account refuses whatever exceeds Always Free. Its 30-day trial and a Pay As You Go
upgrade don't: they bill it. Seven layers keep the project at $0 either way (ADR-0018):

1. Variable validations refuse paid settings in Terraform.
2. Preconditions keep boot + data within 200 GB.
3. `check-free-tier.sh` refuses resource types not known to be free.
4. The tests prove the first three work (all in `make tf-check` and CI).
5. A **quota policy**, `filings-always-free` in the root compartment, which Oracle enforces:
   - Ampere A1 only, at most 2 OCPU and 12 GB in the whole tenancy;
   - block storage at 200 GB and two volumes per availability domain, no backups or replicas;
   - 35 unused services at zero.
6. A **$1 monthly budget** that emails `oci_budget_alert_emails` at the first cent charged and
   when the month's forecast reaches $1. It stops nothing.
7. **`scripts/retry-vm-capacity.sh` refuses to run** without the quota policy, and its plan
   checks let it create only the VM, finish only its volume, attachment, and Atlas entry, and
   grow only the VM in place.

What they leave open:
- Storage spread over several availability domains: there is no region-wide quota, so layer 2
  and the budget cover it.
- Services without quotas, such as MySQL.
- Outbound data past 10 TB a month, far beyond this traffic.

**Before upgrading to Pay As You Go:**
- Governance & Administration, Quota Policies: `filings-always-free` exists, with its
  statements.
- Billing & Cost Management, Budgets: `filings-stay-free` exists, with both alert rules and the
  right email.
- Cost Analysis shows $0 for the month.
- `make tf-plan` shows no change to the quota policy or the budget.

**After upgrading:**
- Check Cost Analysis daily for a week.
- If the budget's email comes, find the charge in Cost Analysis and remove what caused it.
- Never lift a quota to make something fit: anything that needs more costs money.

## Backups and restores

| Data | Where | Backup | If it is lost |
|---|---|---|---|
| Postgres: filings, summaries, search index, users, subscriptions, alerts sent | postgres-0's 10 Gi volume | Nightly `pg_dump --format=custom` at 03:30 UTC to R2: `filings-backups/postgres/filings-<UTC time>.dump`, kept 30 days (Terraform `backup_retention_days`, at most 90) | Restore (below) |
| MongoDB Atlas M0: parsed filings | Atlas | None (M0 has no backups) | Parse again from the raw filings: `filings-replay discovered` (the parser reads R2, not SEC) |
| Raw filings and exhibits | R2 `filings-raw` (no expiry) | None beyond R2's own durability | Public on EDGAR, but no tool fetches them again today |
| Redpanda topics | redpanda-0's volume | None | The relay recreates topics; `filings-replay` republishes from Postgres |
| Valkey | Memory | None | Disposable (rate limits, cache) |
| Sealed Secrets key | kube-system | Exported by hand to a password manager (deploy/k8s/README.md) | Every secret must be sealed again |
| Terraform state | Your machine | Your own copy (infra/terraform/README.md, "State") | `terraform import` |

**Check last night's backup.** No alert watches the backup job, so look now and then:

```
kubectl -n filings get cronjob postgres-backup                                       # LAST SCHEDULE
kubectl -n filings logs -l app.kubernetes.io/name=postgres-backup -c upload --tail=1    # "uploaded postgres/filings-....dump"
```

**Run a backup now** (before a risky change, or to test a new R2 token):

```
kubectl -n filings create job --from=cronjob/postgres-backup postgres-backup-manual
kubectl -n filings logs -f job/postgres-backup-manual -c upload
kubectl -n filings delete job postgres-backup-manual
```

**Restore drill.**

- **Locally: `make backup-drill`** (`scripts/backup-drill.sh`). It dumps the local database the way
  the CronJob does (`pg_dump --format=custom`, with the Postgres image's own tools), restores the
  dump into a new, throwaway database, and compares every table, by row count and by a checksum of
  its rows, with the snapshot the dump was taken from. Then it drops the copy and deletes the dump.
  It only reads the source database; the copy is the only database it writes to. Last run,
  2026-10-07: 14 tables and 10,635 rows (277 filings) from a 14 MB database, a 1.3 MB dump,
  dumped and restored in about a second each: passed.
- **In production,** after the first nightly backup and then every few months: steps 1 to 3 below,
  into `filings_restore`, compare counts with the live database (they differ only by what arrived
  since 03:30), then `DROP DATABASE filings_restore;`.

**Restore in production:**

1. **Get the dump:** the Cloudflare dashboard (R2, `filings-backups`, `postgres/`, the newest
   object, Download), or rclone with the backups token's keys (from your password manager):

   ```
   export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
   export RCLONE_CONFIG_R2_ENDPOINT="$(terraform -chdir=infra/terraform/envs/production output -raw r2_endpoint_url)"
   read -rs RCLONE_CONFIG_R2_ACCESS_KEY_ID && export RCLONE_CONFIG_R2_ACCESS_KEY_ID
   read -rs RCLONE_CONFIG_R2_SECRET_ACCESS_KEY && export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY
   rclone lsl r2:filings-backups/postgres/ | sort -k 2,3 | tail -3
   rclone copyto r2:filings-backups/postgres/filings-<UTC time>.dump filings.dump
   ```

2. **Restore it into a new database** next to the live one; nothing stops:

   ```
   kubectl -n filings exec postgres-0 -- sh -c 'createdb -U "$POSTGRES_USER" -T template0 filings_restore'
   kubectl -n filings exec -i postgres-0 -- \
     sh -c 'pg_restore -U "$POSTGRES_USER" -d filings_restore --exit-on-error --single-transaction' \
     < filings.dump
   ```

3. **Look at it** (psql with `-d filings_restore`):

   ```
   SELECT version_num FROM alembic_version;              -- as in filings, unless a migration ran since
   SELECT status, count(*) FROM filings GROUP BY status;
   SELECT max(created_at) FROM filings;                  -- about the backup's time
   ```

4. **Swap it in** (a few minutes of downtime; read step 5 first): [pause Argo CD and stop the
   writers](#pausing-argo-cd-for-hands-on-work), then rename the databases, then resume:

   ```
   kubectl -n filings exec -i postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1' <<'SQL'
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'filings' AND pid <> pg_backend_pid();
   ALTER DATABASE filings RENAME TO filings_before_restore;
   ALTER DATABASE filings_restore RENAME TO filings;
   SQL
   ```

   The sync after resuming runs the migrations, which bring an older schema up to date.
5. **Afterwards:**
   - Filings that arrived after the backup are found again by the next discovery runs and go
     through the pipeline (within discovery's 30-day lookback: for an older backup, raise
     `DISCOVERY_LOOKBACK_DAYS` for a run). The records of alerts already sent for them were lost
     with the newer data, so their subscribers may get those alerts again (a day's worth, from
     last night's backup). To skip them instead, when the schema revisions matched in step 3,
     leave Argo CD paused after the rename and start everything but the notifier by hand
     (`kubectl -n filings scale deployment api outbox-relay parser summarizer --replicas=1`, and
     the CronJobs). Once the summarizer's lag is 0, skip what the notifier hasn't read, then
     resume Argo CD. This also skips alerts for filings that are new in that window.

     ```
     kubectl -n filings exec redpanda-0 -- rpk group seek notifier --to end --topics filings.ready
     ```

   - Keep `filings_before_restore` until you are satisfied, then drop it.
   - Delete `filings.dump` from your machine.

**After a VM rebuild** (`terraform apply -replace`, or a lost boot disk): the block volume
survives, but the new k3s creates new, empty volumes, so Postgres starts empty and the migrations
create an empty schema. Restore the sealing key first (deploy/k8s/README.md), then the latest
backup (steps 1 to 4). The old cluster's data directories are still on the block volume under
`/mnt/k3s-data/local-path/`, newer than the dump; copying one back is possible, but the dump is
the tested path.

## Capacity planning

Measured with the watchlist of **35 companies, about 3,500 filings a year** (mostly Form 4s;
about 7% are 8-Ks, which is roughly 250 a year):

| Store | Growth per year | Free limit | Room at this rate |
|---|---|---|---|
| MongoDB Atlas M0 (parsed filings) | ~11 MB | 512 MB | ~45 years |
| R2: raw filings | ~80 MB | 10 GB, shared with backups | ~30 years, counting the backups below |
| R2: backups | 30 kept dumps (below) | the same 10 GB | |
| Postgres | ~30 MB | 10 Gi volume | Centuries of disk; the data outgrows `shared_buffers` (192 MB) in about 6 years, which only costs speed |
| Redpanda | A few MB (events are about 200 bytes) | 10 Gi volume | Disk use is set by preallocated segments, not by the events: 32 MiB for each partition's active segment, about 0.7 GB for the 7 topics of 3 partitions (measured locally) |

- **Backups:** dumps compress well (1.3 MB for the 14 MB local database; indexes are rebuilt, not
  stored). Even at a quarter of the database's size, each year of history adds about 7.5 MB per
  dump and 225 MB for the 30 kept. With the raw filings that is about 300 MB of R2 per year of
  history: 3 GB after 10 years, and the 10 GB after about 30.
- **More companies** scale these linearly. Per company and year: MongoDB ~0.31 MB, R2 raw
  ~2.3 MB, Postgres ~0.9 MB. Atlas fills first: at 80% of 512 MB, 100 companies last about
  13 years and 500 about 2.5.
- **SEC:** discovery's run takes about 7 s at 5 requests/s; within its 9-minute deadline one run
  could poll about 2,500 companies.
- **The local LLM:** up to about 5 minutes per 8-K on two laptop cores (the A1 is measured by the
  benchmark), so at most about 21 hours of both cores a year; re-summarizing a year of 8-Ks takes
  up to about a day.
- **Gemini** is only the fallback, so its free daily cap matters only during a long local-model
  outage or a replay with the local model down; the queue absorbs the wait.
- **Grafana Cloud:** about 400 to 1,500 active series of the 10,000 free (ADR-0016). Series grow
  with labels, API routes, and pod restarts, not with filings.
- **VM memory:** 8.97 of the 9 GiB budget, at limits (ADR-0017). A new workload means trimming
  another.

Where to look: Atlas UI (Metrics, data size); `rclone size r2:filings-raw` and
`rclone size r2:filings-backups`; in psql, `SELECT pg_size_pretty(pg_database_size('filings'));`;
`kubectl -n filings exec redpanda-0 -- df -h /var/lib/redpanda/data`; Grafana Cloud's usage
dashboards.

If one nears its limit: parsed payloads in MongoDB can be deleted and re-derived from the raw
filings (`filings-replay discovered`); `backup_retention_days` can be lowered; anything else is a
design decision for an ADR.

### Query speed at volume (measured 2026-10-08)

Every query the system runs regularly, timed at **13 times the volume the 35-company watchlist
reaches in 10 years**: synthetic, production-shaped data for 500 watchlist companies over 10 years
(`scripts/synthetic_data.py`, 8.7 minutes to generate; 2.3 GB): 451,860 filings (93% Form 4s),
1.0 million Form 4 transaction lines, 452,000 summaries and search documents, the newest 50,000
filings embedded, 8,000 companies, 1,000 users with 5,700 subscriptions and 786,000 notifications,
and 462,000 outbox rows (a full replay two days before). `scripts/measure_queries.py` runs the
real code (API endpoints through FastAPI's test client, pipeline handlers in rolled-back
transactions) with production's planner settings, and explains each statement. Laptop numbers:
expect the A1's single Postgres CPU to be about 2 to 3 times slower.

| Query | API p50 / p95 ms | Slowest SQL ms | |
|---|---|---|---|
| Feed, any page or cursor, with or without a form filter | 7–8 / 8–9 | 0.5–1.3 | fixed: was 35–280 ms of SQL |
| Feed or page of one company | 8–9 / 9–16 | 0.4–1.8 | |
| Filing detail (Form 4 with lines, 8-K with exhibits) | 11–14 / 14–30 | 0.1 | |
| Companies: list, watchlist, `q=` | 7–14 / 8–15 | 0.7–7.4 | 8,000 rows, read whole; doesn't grow with history |
| Insider activity, busiest issuer (8,400 Form 4s), 90 or 365 days | 54 / 61–66 | 43 | fixed: was 87–116 ms; grows with the issuer's history |
| Insider trades, all companies: purchases 30 days / sales 90 / sales 365 days | 59 / 85 / 217 (p95 79 / 161 / 240) | 52 / 116 / 230 | grows with `days`; reads `form4_filings` whole, see below |
| Keyword search, a word most documents have (`sold`, `shares`) | 1,290–1,420 / 1,710–2,330 | 1,080–1,480 | **not fixed**, see below |
| Keyword search, selective (`dividend` on 8-Ks, a relaxed query) | 60–63 / 75–81 | 75–80 | |
| Keyword search, ticker of the busiest company / company filter | 128–172 / 158–206 | 77–154 | grows with the company's filings |
| Semantic search: none / company / 8-K filter | 13 / 17 / 247 (p95 14 / 21 / 270) | 1.5 / 11 / 240 | fixed: the 8-K filter returned nothing |
| Hybrid search: none / company / 8-K filter | 106 / 24 / 284 (p95 116 / 35 / 315) | 48 / 9 / 236 | |
| `/me`; `/subscriptions` of the most-subscribed user (25) | 5 / 10 (p95 6 / 11) | 0.3 | |
| Discovery: one run, 499 companies, nothing new | 1,800 / 1,890 | 0.6 | 456 statements; the time is parsing the fake SEC answers |
| Parser (Form 4); summarizer (Form 4 with embedding) | 11 / 19 (p95 13 / 24) | 3.8 | |
| Notifier: a Form 4 with 72 matching subscriptions | 255 / 321 | 0.2 | one transaction per alert, by design (ADR-0012) |
| Relay: a batch of 100; pruning a due batch / nothing due | 10 / 6 / 5 (p95 14 / 7 / 8) | 0.6 / 1.6 / 0.2 | fixed: nothing due read the whole outbox |
| Sign-in (start a session) | 5 / 7 | 0.2 | |

What was fixed (median `EXPLAIN ANALYZE`, before and after, on the same data):

- **Feed** (`GET /filings`): an index on `filings (filed_at, accession_no)`, the feed's order.
  Every page used to sort all filings, partly on disk: first page 249 → 0.4 ms, five years back
  121 → 0.4 ms, 8-Ks only 35 → 0.5 ms. A rare form type (none today) would still walk the index
  until it finds a page of them.
- **Insider activity**: a partial index on `form4_transactions (accession_no)` for the lines the
  endpoint totals (codes P and S, not derivative). The planner probes the issuer's filings' lines
  instead of scanning all 1 million: 79 → 30 ms (90 days) and 87 → 36 ms (365 days). It still
  reads every Form 4 of the issuer, so it grows with the issuer's history, not with `days`;
  bounding it needs a window condition in the query: `filings.filed_at`, as insider trades do,
  not `period_of_report`, which is a filing's earliest trade (a late Form 4 can report year-old
  lines next to last week's). The index's condition must stay the query's.
- **Semantic search with a filter**: pgvector's HNSW scan returns its `hnsw.ef_search` (40)
  nearest vectors and filters apply afterwards. So an 8-K filter returned **0 of 20** results
  (Form 4s filled the 40), and hybrid search got 40 semantic candidates instead of 50. Iterative
  scans (`hnsw.iterative_scan = strict_order`, set per transaction in `semantic_search`) keep going
  until the limit is met: 20 of 20, 50 of 50. The cost is up to `hnsw.max_scan_tuples` (20,000)
  vectors visited: 184–265 ms with these random vectors, where each form forms a tight cluster
  and the query sits nearer the Form 4s; hybrid's 8-K search got 45 of 50 before that cap. Real
  8-K queries sit nearer 8-Ks. Small tables don't use the index (the 278 live embeddings are
  compared exactly), so nothing changed there yet.
- **Outbox pruning**: asking for "the oldest rows published over 7 days ago" read the whole outbox
  whenever none were (after a replay, 460,000 rows for a week, hourly): 91 → 0.2 ms by looking
  only at the oldest batch, since rows are published in id order (ADR-0004).

Not fixed, for a decision when the watchlist grows:

- **Keyword search ranks every matching document.** Postgres computes `ts_rank_cd` for each match
  from the row itself (no index can rank), so the cost follows the number of matches: `sold`
  (171,000 matches) 1.1–1.7 s, `shares` (434,000) 1.3–2.3 s, including 40–170 ms of JIT
  compilation. Template Form 4s tie on rank, so these return the newest matches. On the 35-company
  watchlist after 10 years (35,000 documents) the worst word stays under about 0.3 s. The ticker
  check before a query (`_ticker_words`) also reads every match of a short lowercase word ("sold",
  "share", "chief"): 35–95 ms here. Options, if it matters: rank only the newest N matches (results change only when an older
  match outranks them), a RUM index (an extension the pgvector image lacks), or a search engine
  (ADR-0011 rejected one for this size).
- **Insider trades read `form4_filings` whole** (added 2026-10-08, measured on synthetic data
  made the same way, with fewer embeddings and users, which it doesn't read). The filing-date
  condition finds the window's filings through the feed's index (3,400 for 30 days), but at the
  default `random_page_cost` of 4 the planner prefers a parallel scan of all 420,000
  `form4_filings` rows to 3,400 primary-key probes. With 1.1 (usual for SSDs) it probes: 30 days
  take 25–44 ms instead of about 50. Responses are cached for 60 s, and on the 35-company
  watchlist that table stays 13 times smaller, so this is left as is.
- **Per-company work grows with the company's history**: insider activity (above), and keyword
  search for its ticker or with its filter, which reads all its matching documents (a company
  with 9,400 filings: 77–154 ms).

To measure again (a throwaway database; never the `filings` one):

```
uv run python scripts/synthetic_data.py --create \
    --database-url postgresql+psycopg://filings:filings-local-dev@localhost:5432/synthetic_scale
uv run python scripts/measure_queries.py --plans-dir plans \
    --database-url postgresql+psycopg://filings:filings-local-dev@localhost:5432/synthetic_scale
uv run python scripts/synthetic_data.py --drop --database-url ...
```

The generator resumes where it stopped if interrupted (rerun the same command); `--companies`,
`--years`, `--embeddings`, and `--users` scale it. The measurement flags statements that scan a
large table sequentially, sort on disk, or spend time on JIT; `--only` picks scenarios.

## Enforce the website's Content-Security-Policy

The website's CSP ships as `Content-Security-Policy-Report-Only` (ADR-0015): browsers report
violations in the console without blocking anything. After the first deploy, open the live site,
sign in with Google, use search and the alerts page, and check the console for CSP reports. If
there are none, rename the header to `Content-Security-Policy` in `web/src/lib/securityHeaders.ts`
and redeploy. If a violation is expected (for example Google changed a domain), add that source to
the policy first.
