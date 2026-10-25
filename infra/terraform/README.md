# Terraform: the production environment

Everything the system needs outside its Kubernetes cluster, for $0 a month: the Oracle Cloud VM
and its network, Cloudflare (tunnel, DNS, R2, Pages), MongoDB Atlas, and Grafana Cloud's
dashboards and alerts. Argo CD deploys everything inside the cluster from `deploy/`. The design
and its reasons are in [ADR-0018](../../docs/adr/0018-terraform.md).

**Nothing here has been applied yet.** No accounts exist, and the code is checked only with
`terraform validate` and tests against mock providers. The first real apply may still surface
things only a live account shows: A1 capacity, which Atlas regions offer M0, the Pages GitHub
link, the Grafana Cloud data source UIDs.

```
modules/oci/          network, Ampere A1 VM, block volume, cloud-init (k3s + Argo CD)
modules/cloudflare/   tunnel to the API, DNS records, R2 buckets, Pages project
modules/atlas/        project, M0 cluster, database user, IP access list
modules/grafana/      dashboards and alert rules from deploy/grafana
envs/production/      the root module that wires them together; tests/ holds the tests
tools/capacity-report/  asks Oracle whether an A1 size fits, creating nothing (ADR-0020)
check-free-tier.sh    fails on any resource type not on the free-tier allowlist
```

## Checking the code (no accounts needed)

```
make tf-check   # fmt, init -backend=false, validate, terraform test, free-tier allowlist
make tf-fmt     # format the code
```

`terraform test` runs the files in `envs/production/tests/` against **mock providers**: plans
are computed locally, no credentials are read, and nothing is created. They check the free-tier
guardrails (an `ocpus = 4` or an `M10` cluster must be refused), the cloud-init file, the tunnel
routing, the Pages build, and the translation of `deploy/grafana`'s files. CI runs the same.

The providers are large (about 700 MB), so `make tf-check` keeps one copy in
`~/.terraform.d/plugin-cache` for every checkout.

## Free-tier guardrails

Validations refuse anything outside the free tiers (brief §9, checked October 2026; re-verify
before applying), so a typo fails the plan instead of creating a bill:

- **Oracle:** only `VM.Standard.A1.Flex`, at most 2 OCPU and 12 GB; volumes at Balanced
  performance only, boot + data within 200 GB (`free_block_storage_gbs`); no load balancer, NAT
  gateway, or reserved IP (the allowlist).
- **Atlas:** only `M0` on the shared `TENANT` provider.
- **Cloudflare:** R2's Standard class only, backups expire (at most 90 days); Pages builds only
  when `web/` changes, with previews off.
- **Access:** SSH and Atlas never open to `0.0.0.0/0` (nothing wider than a /16).

The accounts matter as much as the code: **never upgrade the Oracle account to pay-as-you-go**
(an upgraded account bills what exceeds the allowance instead of refusing it), and set a
budget alert in each account that has a card on file.

## What a first apply needs

### Decisions first (brief §12)

- **The domain.** The tunnel's stable hostname, the website, and the sign-in cookie all need one
  domain on Cloudflare (about $10 a year, the project's only accepted cost). It is the
  `domain` variable; the API is `api.<domain>` and the website `app.<domain>` by default.
  It can come later (2026-10-22): leave `domain` unset and the first apply makes everything
  else, with the tunnel serving nothing yet and no DNS records or website. The pipeline and its
  Discord alerts need no domain. The apply that sets it adds the rest; the Cloudflare token then
  needs the zone permissions below too.
- ~~**R2 and a payment method.**~~ Decided 2026-10-21: R2, with a payment method on file.
  Cloudflare asks for one when R2 is first enabled, even on the free tier; usage stays far below
  it (raw filings were 7.5 MB for a month of the watchlist, and the database 17 MB). R2 keeps the
  backups off the provider that runs the VM, and reads out of it are free.
- **The Oracle home region.** Chosen at sign-up and permanent; Always Free resources exist only
  there. Pick one near you with a reputation for A1 capacity.
- ~~**Where the state lives**~~ Decided 2026-10-22: the R2 bucket `filings-tfstate` (see
  [State](#state)).
- ~~**Pages from GitHub or from CI**~~ Decided 2026-10-21: from GitHub
  (`pages_github_integration`, the default). Cloudflare builds the site on every push to `main`
  that changes `web/`, with the Pages GitHub app installed on the repository; no Cloudflare token
  goes into GitHub's secrets. A Wrangler deploy from CI would gate the site on CI directly, but a
  project can't switch between the two later.

### Accounts and credentials (manual, once)

**The GitHub app, without a Worker.** Connecting Git from the Workers side of "Create" makes a
Worker project that builds every push of this repository and fails ("Workers Builds" checks on
GitHub). If one was made, delete the Worker (its Settings) and the build connection it leaves,
or remove the repository from the app (GitHub, Settings, Applications) until the Pages project
exists. Terraform makes the Pages project; nothing else needs to be created in the dashboard.

Each is an account action, which is why Terraform doesn't do it (ADR-0018, "What stays manual").

| Service | Create by hand | Terraform reads it from |
|---|---|---|
| Oracle Cloud | Free Tier account; an API signing key for your user (Console: My profile, API keys, or `oci setup config`) | `~/.oci/config` (profile `oci_config_file_profile`); `oci_tenancy_ocid` and `oci_region` in tfvars |
| Cloudflare | Account; the domain added as a zone (nameservers switched, zone active); R2 enabled; the Pages GitHub app installed on the repository (Workers & Pages, Create, the **Pages** tab, Connect to Git, once; then leave without creating a project); an API token (below) | `CLOUDFLARE_API_TOKEN`; `cloudflare_account_id` and `domain` in tfvars |
| MongoDB Atlas | Account and organization; an organization service account with the Organization Project Creator role | `MONGODB_ATLAS_CLIENT_ID`, `MONGODB_ATLAS_CLIENT_SECRET`; `atlas_org_id` in tfvars |
| Grafana Cloud | Account and a stack; a service account in the stack with the Admin role (alerting provisioning needs it) and a token | `GRAFANA_AUTH`; `grafana_url` in tfvars |
| Google Cloud | Add `https://app.<domain>` to the existing OAuth client's authorized JavaScript origins | not used by Terraform |

The Cloudflare API token for Terraform (My Profile, API Tokens, custom token):

- Account: **Cloudflare Tunnel: Edit**, **Workers R2 Storage: Edit**, **Cloudflare Pages: Edit**
- Zone (only the project's zone): **DNS: Edit**, **Zone: Read**

If the Atlas organization requires an IP access list for its API, add your own address to the
service account's list.

Also made by hand, for the cluster rather than for Terraform (see
[Secrets into the cluster](#secrets-into-the-cluster)): two **R2 API tokens** (Object Read &
Write, one per bucket) and a **Grafana Cloud access policy token** for the OpenTelemetry
Collector (metrics, logs, and traces write). Terraform creates no Grafana contact point: alerts
go to the stack's default one (email to the account owner) until you add another, such as a
Discord webhook, in Grafana Cloud.

### The repository

cloud-init clones this public repository at `git_revision` (default `main`) and applies
`deploy/argocd/install` and `deploy/argocd`, so the Argo CD manifests must be on that branch
first. Leave it at `main`, where they are edited. From then on Argo CD deploys the Sealed Secrets
controller from `main` and the application from the `production` branch, which the image
workflow's deploy job creates on its first run and moves on every deploy
([deploy/k8s/README.md](../../deploy/k8s/README.md#deploying)). If the branch doesn't exist yet
when the VM boots, run that workflow by hand (**Actions → Images → Run workflow**); the
application finds the branch on a later refresh. No credential is needed while the repository is
public. If it ever becomes private, Argo CD needs a read-only deploy key (a repository Secret,
sealed) and the bootstrap's clone needs one too.

## First apply

```
cd infra/terraform/envs/production
cp production.tfvars.example production.tfvars                # gitignored; fill in the IDs
cp production.s3.tfbackend.example production.s3.tfbackend    # gitignored; the account ID
cd -
# Credentials, one per line as `export NAME=value`, in ~/.config/filings/terraform.env (chmod 600):
#   CLOUDFLARE_API_TOKEN, MONGODB_ATLAS_CLIENT_ID, MONGODB_ATLAS_CLIENT_SECRET, GRAFANA_AUTH,
#   and the state bucket's R2 token as AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
make tf-init     # terraform init -backend-config=production.s3.tfbackend
make tf-plan     # terraform plan -var-file=production.tfvars -out=production.tfplan
make tf-apply    # terraform apply production.tfplan: exactly what the plan showed
```

Read the plan before applying it: one A1 instance with 2 OCPU and 12 GB (or 1 and 6 with
`oci_instance_ocpus` and `oci_instance_memory_gbs`; ADR-0020), one 100 GB volume,
networking, two R2 buckets, a tunnel, an M0 cluster, and the Grafana folder, dashboards, and rule
groups, plus two DNS records and a Pages project once `domain` is set. Nothing else.

- **"Out of host capacity"** when creating the instance is Oracle running out of A1 hosts, not
  a configuration error. Everything else is already created; run `terraform apply` again later,
  or try another `oci_availability_domain_number` in regions that have several, or let
  `scripts/retry-vm-capacity.sh` retry at both sizes and grow a smaller VM later
  ([runbook](../../docs/runbook.md#oracle-idle-reclamation-and-capacity-errors)).
- **The VM bootstraps itself** in roughly 10 minutes: it mounts the block volume, installs k3s
  and Argo CD, and Argo CD starts syncing (the application from the `production` branch; see
  [The repository](#the-repository)). Its log is `/var/log/cloud-init-output.log`, readable
  through the OCI console's serial console or over SSH.
- **Idle reclamation:** Oracle may reclaim an Always Free VM whose CPU, network, and memory use
  all stay under 20% for 7 days. The running stack holds most of the memory, but a stopped VM
  qualifies.

### Reaching the cluster

Nothing listens on the VM's public IP. For maintenance, set `ssh_ingress_cidr` to your address
(`x.x.x.x/32`) with `ssh_authorized_keys` set, apply, and tunnel to the Kubernetes API:

```
ssh -L 6443:127.0.0.1:6443 ubuntu@$(terraform output -raw vm_public_ip)
# in another terminal, once: copy the kubeconfig (only root can read it) to ~/.kube/config
mkdir -p ~/.kube
ssh ubuntu@$(terraform output -raw vm_public_ip) sudo cat /etc/rancher/k3s/k3s.yaml > ~/.kube/config
```

Set `ssh_ingress_cidr` back to null and apply again when done.

## Secrets into the cluster

Terraform's generated secrets reach the cluster as SealedSecrets committed to git, never through
the VM's user data (which pods can read). The general procedure is in
[deploy/k8s/README.md](../../deploy/k8s/README.md); fetch the sealing certificate first
(`kubeseal --fetch-cert`, through the SSH tunnel). For the values Terraform knows, pipe them
straight into `kubeseal`, so no plain copy touches the disk or a command line:

```
cert=../../../../deploy/k8s/overlays/production/sealed-secrets/cert.pem
out=../../../../deploy/k8s/overlays/production/sealed-secrets

terraform output -raw tunnel_token \
  | kubectl create secret generic cloudflared -n filings --dry-run=client -o yaml \
      --from-file=TUNNEL_TOKEN=/dev/stdin \
  | kubeseal --cert "$cert" --format yaml > "$out/cloudflared.yaml"

terraform output -raw mongodb_url \
  | kubectl create secret generic mongodb -n filings --dry-run=client -o yaml \
      --from-file=MONGODB_URL=/dev/stdin \
  | kubeseal --cert "$cert" --format yaml > "$out/mongodb.yaml"
```

| Secret | Values from |
|---|---|
| `cloudflared` | `terraform output -raw tunnel_token` |
| `mongodb` | `terraform output -raw mongodb_url` |
| `r2-raw-filings`, `r2-backups` | endpoint: `terraform output r2_endpoint_url`; keys: the R2 API tokens made by hand, one per bucket |
| `grafana-cloud` | the Grafana Cloud portal (OTLP endpoint, instance ID, access policy token) |
| `cloudflare-analytics` | the account ID, and a token made by hand with only **Account Analytics: Read** (the daily R2 free-tier check) |
| `postgres` | generate: `openssl rand -hex 32` |
| `gemini`, `sec-edgar`, `smtp` | your own values |

Then the non-secret settings in `deploy/k8s/overlays/production/config.env`: `API_CORS_ORIGINS`
is `terraform output -raw api_cors_origins`, and `GOOGLE_CLIENT_ID` is the same public ID as the
`google_client_id` variable. List the sealed files in the overlay's `kustomization.yaml`, commit,
and push to `main`; once CI passes, the image workflow deploys them to `production`, and Argo CD
does the rest.

**Back up the sealing key** right after the first sync (deploy/k8s/README.md shows the command)
and keep it outside git. After a VM rebuild, restore it before sealing anything new:
`kubectl apply -f sealed-secrets-key.yaml`, then
`kubectl -n kube-system rollout restart deployment/sealed-secrets-controller`.

## State

Terraform's state records what it created, and it **contains secrets** (the Atlas password, the
tunnel token). It lives in a private R2 bucket, `filings-tfstate` (decided 2026-10-22): off the
laptop and off Oracle, free, and locked during each run with a lock file (R2 supports the
conditional writes that needs). The bucket and its token are made by hand once, because
Terraform needs them before it can run:

1. In R2, create the bucket `filings-tfstate`, then an API token with **Object Read & Write**
   on that bucket only. Export its Access Key ID and Secret Access Key as `AWS_ACCESS_KEY_ID`
   and `AWS_SECRET_ACCESS_KEY`.
2. `cp production.s3.tfbackend.example production.s3.tfbackend` (gitignored) and put your
   Cloudflare account ID in the endpoint.
3. `terraform init -backend-config=production.s3.tfbackend`.

The other options considered were a local file (lost with the laptop), OCI Object Storage (lost
with the Oracle account it describes), HCP Terraform's free plan (another vendor, Terraform
only), and OpenTofu's state encryption (a passphrase to never lose). R2 doesn't version objects,
so a state that matters is worth a copy (`terraform state pull > state.json`, kept private) before
risky changes.

## Later changes

- **Re-apply** after editing variables: `make tf-plan`, read it, then `make tf-apply`. A bare
  `terraform apply` doesn't read `production.tfvars` and asks for every variable.
- **cloud-init changes don't reach the running VM** (the instance ignores them, so a new Ubuntu
  image or a bootstrap edit can't replace it by accident). Rebuild deliberately with
  `terraform apply -replace=module.oci.oci_core_instance.k3s`: the block volume and its data
  survive, the public IP changes, and Terraform updates the Atlas access list.
- **Provider upgrades:** `terraform init -upgrade`, run `make tf-check`, review and commit
  `.terraform.lock.hcl`.
- **Tearing down:** `terraform destroy` deletes the Atlas cluster (the parsed filings) and the
  VM; R2 buckets must be emptied first.
