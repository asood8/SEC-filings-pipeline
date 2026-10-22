# ADR-0018: Infrastructure as code with Terraform

- **Status:** Accepted (2026-10-22)
- **Date:** 2026-10-07

## Context

Phase 7 puts the system in the cloud. Everything inside the Kubernetes cluster is deployed by
Argo CD from `deploy/k8s` (ADR-0017). Everything outside it needs to exist first:

- **Oracle Cloud:** the network and one Ampere A1 VM (arm64, 2 OCPU, 12 GB) with a block volume,
  which must boot into a working k3s cluster with Argo CD.
- **Cloudflare:** the tunnel that publishes the API, DNS records, R2 buckets for raw filings and
  backups, and the Pages project for the website.
- **MongoDB Atlas:** the M0 cluster for parsed filings, its database user, and its IP access list.
- **Grafana Cloud:** the dashboards and alert rules, from `deploy/grafana`.

The brief asks for Terraform (or OpenTofu) for all of it, so the system can be rebuilt elsewhere.
The constraints:

- **$0 a month.** Every resource must be inside a free tier (brief §9). A typo such as `ocpus = 4`
  must be refused, not billed.
- **The repository is public.** No credential, state file, or variable file with real values may
  be committed.
- **No accounts exist yet**, and creating them is the user's call. This phase writes and validates
  the code; nothing is planned or applied against a real provider.
- **Free tiers change.** Provider-specific code stays in one module per provider, so a pivot
  (brief §11) replaces a module, not the design.

## Decision

We will manage all cloud resources outside the cluster with **Terraform**, in `infra/terraform`:
four reusable modules and one root module for production. Nothing outside the cluster is
configured by hand except the account-level steps listed below.

### Layout

```
infra/terraform/
  modules/oci/          VCN, subnet, gateway, routes, security lists, A1 VM, block volume, cloud-init
  modules/cloudflare/   tunnel + its routing, DNS records, R2 buckets + lifecycle, Pages project + domain
  modules/atlas/        project, M0 cluster, database user, IP access list
  modules/grafana/      folders, dashboards, alert rule groups (read from deploy/grafana)
  envs/production/      versions.tf, providers.tf, main.tf, variables.tf, outputs.tf,
                        production.tfvars.example, .terraform.lock.hcl
  envs/production/tests/  `terraform test` files, run against mock providers
  check-free-tier.sh    resource-type allowlist (see guardrails)
  README.md             operator guide: accounts, tokens, first apply, secrets
```

The root module only wires modules together and passes values between them, for example the VM's
public IP into the Atlas access list. Another environment would be another directory under `envs/`.

### What is managed, and why each piece is free

| Module | Resources | Free-tier basis (brief §9) |
|---|---|---|
| oci | VCN, public subnet, internet gateway, route table, security list, emptied default security list | Networking objects are free. No NAT gateway, service gateway, load balancer, or reserved IP. |
| oci | One `VM.Standard.A1.Flex` instance, 2 OCPU, 12 GB, Ubuntu 24.04 aarch64, 50 GB boot volume | The whole Always Free A1 allowance. |
| oci | One 100 GB block volume at Balanced performance (10 VPUs/GB), paravirtualized attachment | Boot + data = 150 of 200 free GB. No volume backups (Always Free has five); backups go to R2. |
| cloudflare | Remotely managed tunnel, its routing (`api.<domain>` to `http://api.filings.svc.cluster.local:8000`), proxied CNAME | Tunnel and DNS are free. |
| cloudflare | R2 buckets `filings-raw` and `filings-backups` (Standard class), backups expire after 30 days | 10 GB-month of Standard storage free; the expiry keeps backups bounded. |
| cloudflare | Pages project (root `web/`, `npm ci && npm run build`, output `web/dist`, `VITE_API_BASE_URL`, `GOOGLE_CLIENT_ID`), custom domain `app.<domain>` | Pages is free (500 builds a month); builds run only when `web/**` changes, and previews are off. |
| atlas | Project, `M0` cluster on the `TENANT` provider, database user (`readWrite` on `filings`, scoped to the cluster), access list with the VM's IP | M0 is free forever (512 MB). |
| grafana | Folders, one dashboard per `deploy/grafana/dashboards/*.json`, one rule group per group in `deploy/grafana/alerting/*.yaml` | Dashboards and rules are free; the limits apply to telemetry. |

The tunnel route points at the `api` Service on port 8000 in the `filings` namespace, as the
manifests define it; all three are module variables. The website and the API share the domain,
which the session cookie requires (ADR-0013).

### Free-tier guardrails

Four layers, all of which run without credentials:

1. **Variable validations** refuse paid settings with an explanation:
   - the shape must be `VM.Standard.A1.Flex`, with at most 2 OCPU and 12 GB, in whole units;
   - volumes must be at least 50 GB each (OCI's minimum), at Balanced performance only;
   - the Atlas tier must be `M0` on the `TENANT` provider;
   - R2 buckets must use the Standard class, and backup retention is capped at 90 days;
   - SSH and the Atlas access list accept only IPv4 blocks of /16 or narrower, so never
     `0.0.0.0/0`.
2. **Preconditions** check what one variable can't see alone: the boot volume plus the data volume
   must fit in `free_block_storage_gbs` (200 by default; lower it if the tenancy has other
   volumes), and Atlas gets a second M0/TENANT check on the cluster itself.
3. **`check-free-tier.sh`**, run by `make tf-check` and CI, fails if any `.tf` file declares a
   resource type that isn't on its allowlist of the 24 types above, or uses a module from a
   registry or URL. Adding a type (a load balancer, a NAT gateway, a paid Cloudflare feature) then
   has to be a deliberate change to that list.
4. **Tests** (`terraform test`, below) prove the first two layers work: each paid configuration
   (`ocpus = 4`, 24 GB, a non-A1 shape, 250 GB of volumes, Higher Performance storage, `M10`, a
   dedicated Atlas provider, Infrequent Access storage, SSH or Atlas open to the internet) must
   fail the plan.

The account settings matter as much as the code: the Oracle account must stay on the Free tier
(an upgraded account bills what exceeds the allowance instead of refusing it), and Cloudflare
asks for a payment method when R2 is enabled, even on the free tier (an open decision in the
README).

### Bootstrapping the VM: cloud-init

The VM's user data is a cloud-init file (`modules/oci/cloud-init.yaml.tftpl`) that runs once, on
the first boot, and a bootstrap script (`modules/oci/bootstrap.sh`) that it installs and runs:

1. **Host firewall.** Oracle's Ubuntu images reject all inbound and forwarded traffic except SSH
   with iptables. Those rules stay, plus an exception for k3s's pod network, without which pods
   can't reach each other or CoreDNS.
2. **Block volume.** It waits for the volume (Terraform attaches it after the VM starts), formats
   it only if it is empty, and mounts it by UUID at `/mnt/k3s-data`. k3s's local-path provisioner
   stores every persistent volume there, and a systemd drop-in keeps k3s from starting without
   the mount, so data never lands on the boot disk by mistake.
3. **k3s**, pinned (`v1.36.5+k3s1`, the stable channel in October 2026), from the installer at
   the same release tag, which verifies the binary's checksum. Traefik and the service load
   balancer are disabled: cloudflared reaches the `api` Service directly, and nothing listens on
   the VM. Secrets are encrypted at rest in k3s's datastore.
4. **Argo CD**, from the repository at `git_revision`: `kubectl apply --server-side
   --force-conflicts -k deploy/argocd/install` (Argo CD's own install, which pins its version and
   trims it for this node), then `kubectl apply -k deploy/argocd` (the project and the
   Applications). From then on
   Argo CD syncs everything else from git: the Sealed Secrets controller from `main`, and the
   application from the `production` branch that the image workflow writes (ADR-0019, since
   2026-10-08), so `git_revision` stays `main`.

Each step is idempotent, so the script can be re-run by hand after a failure. The repository is
public, so neither cloud-init nor Argo CD needs a credential to clone it. If it ever became
private, Argo CD would need a read-only deploy key (a repository Secret, sealed), and the
bootstrap clone would need one too.

**User data never contains a secret.** Anything running on the VM, pods included, can read it from
the instance metadata service. The VM accepts only the metadata service's v2 endpoints.

The instance ignores later changes to its image and user data. Otherwise a new Ubuntu image or an
edited cloud-init file would replace the VM: k3s state and the public IP would be lost, and A1
capacity might not be available to create it again. Rebuilding is a deliberate
`terraform apply -replace=module.oci.oci_core_instance.k3s`; the block volume survives it.

### Credentials and secrets

- **Provider credentials** are sensitive variables that default to null, so each provider falls
  back to its own environment variable (`CLOUDFLARE_API_TOKEN`, `MONGODB_ATLAS_CLIENT_ID` and
  `_SECRET`, `GRAFANA_AUTH`) or, for OCI, the API key in `~/.oci/config`. Exporting them in the
  shell keeps them off the disk; a `*.tfvars` file also works and is gitignored.
- **`production.tfvars.example`** is the committed template, with placeholders only. The filled-in
  copy is `production.tfvars`, not `terraform.tfvars`, because `terraform test` loads the latter
  automatically and the tests must run on their own inputs.
- **Generated secrets** (the Atlas password, the tunnel token) are sensitive outputs. They reach
  the cluster as SealedSecrets: the operator pipes `terraform output -raw` into `kubectl create
  secret --dry-run=client` and `kubeseal`, and commits only the sealed result (README,
  "Secrets into the cluster"). They never pass through the VM's user data.

### State

Terraform records what it created in a **state file**, which contains those generated secrets. The
default is the **local backend** (there is no backend block): `envs/production/terraform.tfstate`,
gitignored. That suits one operator, but the file must be kept private and backed up, because
losing it means Terraform no longer knows what it manages (recoverable with `terraform import`,
but tedious). Leaving the backend block out also lets `init -backend=false`, `validate`, and
`test` run with no backend configured at all.

*Decided 2026-10-22:* the state moved to a private R2 bucket, `filings-tfstate`, through the
`s3` backend with Terraform's lock file (R2 supports the conditional writes it needs). The bucket
and its token are made by hand; the account's endpoint stays out of git in a `.tfbackend` file
(infra/terraform/README.md, "State"). The free options that were considered:

- **OCI Object Storage** (Always Free includes 20 GB), in a private bucket made by hand, through
  Terraform's native `oci` backend or the S3-compatible `s3` backend (which OpenTofu also has).
- **Cloudflare R2** through the same S3-compatible backend, in its own bucket.
- **HCP Terraform's free plan**, which stores state remotely and can run plans.
- **OpenTofu with client-side state encryption**, which encrypts the state before it is written
  anywhere (an OpenTofu-only feature).

### Versions and OpenTofu

- **Terraform:** the code requires `>= 1.10, < 2.0`. CI pins **1.16.5**, the current release in
  October 2026, with `hashicorp/setup-terraform@v4`.
- **Providers:** `oracle/oci ~> 9.8`, `cloudflare/cloudflare ~> 5.27`, `mongodb/mongodbatlas
  ~> 2.19`, `grafana/grafana ~> 4.47`, `hashicorp/random ~> 3.9`: new minor releases of the same
  major version only. The exact versions and checksums are in `.terraform.lock.hcl`, which is
  committed; checks run `init -lockfile=readonly`, so they install exactly those. No `cloudinit`
  provider: `templatefile()` and `base64encode()` build the user data.
- **OpenTofu-compatible.** The code uses only features OpenTofu also has (1.10 and later satisfy
  the version constraint). Switching means installing `tofu` and running `tofu init`, which
  writes its own lock file for `registry.opentofu.org`.

### What stays manual, and why

Each of these is an account action (identity, payment terms, or a grant of access) rather than
configuration, so automating it would mean giving Terraform far broader credentials for a
one-time step:

- **Creating the accounts**: Oracle Cloud (the home region is permanent), Cloudflare, MongoDB
  Atlas (and its organization), Grafana Cloud (and its stack).
- **The domain**: registering it, adding it to Cloudflare, pointing its nameservers there.
- **Terraform's own credentials**: the OCI API signing key, the Cloudflare API token, the Atlas
  service account, the Grafana service account token.
- **Enabling R2** and creating the **R2 API tokens** the services use (one per bucket). Creating
  tokens from Terraform would need a token that can create tokens.
- **Installing the Cloudflare Pages GitHub app** on the repository, before the first apply.
- **The Grafana Cloud OTLP access policy token** for the OpenTelemetry Collector, and contact
  points beyond the default email (they usually contain webhook secrets).
- **Adding the production origin** (`https://app.<domain>`) to the Google OAuth client.

### Checks and tests

`make tf-check` runs `terraform fmt -recursive -check`, `terraform init -backend=false` and
`terraform validate` for the root module (which validates every module it calls), `terraform
test`, and `check-free-tier.sh`. CI runs the same in its own job, with no credentials, caching
providers by the lock file's hash.

The tests (`envs/production/tests/*.tftest.hcl`) use Terraform's **mock providers**: each run
computes a plan (one Atlas run an apply, in memory) from fake provider responses, so it needs no
account and creates nothing.
One file per module, plus one for the root module's wiring. Beyond the guardrails, they check
that the cloud-init file is valid YAML that pins k3s, disables Traefik, and embeds the bootstrap
script intact; that inbound traffic is closed unless SSH is opened; that the tunnel routes
`api.<domain>` to the `api` Service; that Pages builds `web/` with the public settings; that the
Atlas connection string carries its credentials; that the VM's IP reaches the Atlas access list;
and that Grafana's provisioning files translate correctly (fixtures in `tests/fixtures/grafana`,
and the root test checks every file in `deploy/grafana/dashboards`).

### Grafana: one set of files for both viewers

The dashboards and alert rules in `deploy/grafana` are in Grafana's own provisioning formats, so
the local viewer loads them as they are and this module translates them:

- **Dashboards** choose their data sources through template variables, which resolve to the
  stack's default Prometheus and Tempo, so they are uploaded unchanged. A panel that names the
  local viewer's data source directly (`"uid": "prometheus"`) is rewritten to the stack's.
- **Alert rules** name the local viewer's data source (`datasourceUid: prometheus`), which is
  replaced by the stack's (`grafanacloud-prom` by default, a variable). Group intervals become
  seconds, and a rule's `dashboardUid` and `panelId` become the two link annotations the API
  expects. The API refuses one without the other, so a rule naming only a dashboard gets no link.

## Alternatives considered

- **OpenTofu instead of Terraform.** The same language, an open-source license, and client-side
  state encryption. Terraform is what the brief names first and what most teams run, and the code
  stays compatible, so the choice can be reversed with a new lock file.
- **Pulumi.** Infrastructure in Python would match the services, but it adds an SDK and a state
  service, and the brief asks for Terraform.
- **Ansible (or another configuration tool) for the VM.** It would need SSH open to a control
  machine. cloud-init is built into the image and needs no inbound access at all.
- **Terraform's Kubernetes or Helm providers for in-cluster resources.** They would need the
  cluster's API reachable from the operator's machine, and two tools would own cluster state.
  Argo CD owns everything inside the cluster; Terraform stops at the VM.
- **Registry modules** (for example Oracle's VCN module). They create more than this needs (NAT
  and service gateways) and hide resources from the allowlist, so modules are local only.
- **iSCSI volume attachment.** Faster in some cases, but the guest must run `iscsiadm` login
  commands. Paravirtualized attachment shows up as a plain disk.
- **A reserved public IP.** The ephemeral IP stays with the VM across stops and restarts; only a
  rebuild changes it, and Terraform then updates the Atlas access list.
- **Generating the Sealed Secrets key in Terraform**, so secrets could be sealed before the cluster
  exists and would survive a rebuild. The private key would then sit in the state file and in the
  VM's user data, which pods can read, and anyone with it could decrypt every sealed secret in
  this public repository. Instead the controller generates its key, and the operator backs it up
and restores it by hand (over the SSH tunnel) after a rebuild.
- **Terraform creating the Grafana Cloud stack, R2 tokens, or the Pages GitHub link.** Each needs
  an organization-level or token-creating credential for a one-time action; see above.

## Consequences

- **The system is rebuildable:** `terraform apply`, then sealing the secrets (a handful of
  commands), and Argo CD brings up the rest. The first apply still needs the manual steps above.
- **`terraform validate` and mock-provider tests are not an apply.** They prove the code is well
  formed against the providers' schemas and does what it means to, not that the APIs accept it. The first real apply may surface things only a
  live account shows: A1 capacity, which regions offer M0, the Pages GitHub link, Grafana Cloud's
  data source UIDs.
- **Local state is a single point of failure** until the state decision is made: back it up, and
  remember it holds secrets.
- **cloud-init changes don't reach the running VM**, by design (`ignore_changes`). After editing
  the bootstrap, rebuild the VM deliberately or apply the change by hand.
- **The VM is reachable only through Cloudflare** (and SSH while `ssh_ingress_cidr` is set). Day-to-
  day operations go through Argo CD and git; break-glass access is SSH opened for a while, or
  OCI's serial console.
- **Dashboards and rules have one source of truth** in `deploy/grafana`, shared with the local
  viewer. The stack's data source UIDs default to Grafana Cloud's usual ones and should be checked
  once.
- **Provider upgrades are deliberate**: `terraform init -upgrade`, then review the lock file. The
  Cloudflare provider in particular rewrote many resources in v5.
- **Revisit** if the free tiers change (brief §11's fallback pivots replace the oci module), if a
  second operator joins (remote state with locking becomes necessary), or if a domain is chosen
  that isn't on Cloudflare.

## Tools in plain language

- **Terraform:** a tool that reads files describing the infrastructure you want ("one VM of this
  size, one DNS record") and makes the real infrastructure match, by calling each cloud's API.
  `terraform plan` shows what it would change; `terraform apply` does it.
- **OpenTofu:** an open-source fork of Terraform that reads the same files, maintained by the
  Linux Foundation.
- **Provider:** a plugin that teaches Terraform one service's API (Oracle Cloud, Cloudflare,
  Atlas, Grafana). Terraform downloads providers from its registry, at the versions pinned in the
  lock file.
- **Module:** a reusable folder of Terraform files with inputs (variables) and outputs, like a
  function. Here, one per provider; the production environment calls all four.
- **State:** Terraform's record of which real resources it created for which lines of code, so it
  knows what to change or delete next time. It can contain secrets.
- **Lock file (`.terraform.lock.hcl`):** the exact provider versions and their checksums, so every
  machine and CI run uses identical providers.
- **Variable validation and preconditions:** rules checked before anything is created. Here they
  are what refuses paid configurations.
- **cloud-init:** the standard first-boot agent on cloud Linux images. It reads instructions
  supplied when the VM is created (user data) and runs them once: here, mounting the disk and
  installing k3s and Argo CD.
- **k3s:** a lightweight, single-binary Kubernetes distribution suited to one small node.
- **`terraform test` and mock providers:** Terraform's built-in test runner. A test file lists
  runs (a plan with some variables) and assertions about the result; a mock provider stands in
  for the real one and invents plausible responses, so the tests need no account.
