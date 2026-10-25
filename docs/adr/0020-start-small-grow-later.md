# ADR-0020: Starting on a smaller VM and growing it in place

- **Status:** Accepted (2026-10-25)
- **Date:** 2026-10-25

## Context

Production runs on one Oracle Ampere A1 VM with 2 OCPU and 12 GB, the whole Always Free A1
allowance (brief §9; ADR-0017, ADR-0018). Everything else was created on 2026-10-22, but the VM
wasn't: Oracle answered "Out of host capacity" to about 2,700 attempts over three days, in all
three availability domains (`scripts/retry-vm-capacity.sh`). Phase 7 can't finish without a VM.

A compute capacity report (below) on 2026-10-25 said the same for every domain and for both
sizes, 2 OCPU/12 GB and 1 OCPU/6 GB. Oracle often has room for the smaller size sooner, though,
and nothing about the project requires the larger one on day one.

The constraints:

- **$0 a month.** The allowance is 2 OCPU and 12 GB *in total*. A second, smaller VM beside the
  full one would exceed it. The account's service limits are far higher only while its free
  trial lasts, and what the trial allows beyond Always Free is shut down when it ends.
- **Growing reboots the VM.** Oracle can change a flexible VM's OCPUs and memory in place,
  keeping its disks and public IP, but "if the instance is running when you change the shape, it
  is rebooted as part of the change shape operation" (Oracle's *Changing the Shape of an
  Instance*). The docs don't say whether a resize that fails for capacity still reboots it, so
  retrying a resize every 30 seconds could restart production over and over.
- **The stack doesn't fit in 6 GB as it is.** At their limits the workloads need 7.1 GiB, plus
  about 1.4 GiB for k3s and Argo CD. The local LLM (3 GiB) and the embedding server (0.5 GiB) are
  most of it.

## Decision

**Start on 1 OCPU and 6 GB if that comes first, and grow the same VM to 2 OCPU and 12 GB later.**

- **Terraform** takes the size as two variables, `oci_instance_ocpus` and
  `oci_instance_memory_gbs` (2 and 12 by default). The module's validations still refuse more
  than the allowance.
- **`scripts/retry-vm-capacity.sh`** tries the sizes in `SIZES` (default `"2:12 1:6"`) in turn
  in each availability domain, largest first, and records the one it gets in
  `production.tfvars`. If that is the smaller one, it then waits to grow it. Every
  `GROW_INTERVAL` seconds (5 minutes) it asks for a **compute capacity report** for 2 OCPU and
  12 GB in the VM's domain. Only when Oracle answers `AVAILABLE` does it apply a plan that it
  has checked updates the instance in place and does nothing else. The one reboot that follows
  is the price of growing. The report comes from `infra/terraform/tools/capacity-report`, a tiny
  Terraform configuration applied with a throwaway local state. Nothing about it touches
  production's state.
- **Kubernetes** gets a component, `deploy/k8s/components/small-vm`, which the production
  overlay lists while the VM is small:
  - it removes the local LLM and the embedding server (their Deployments, Services, network
    policies, and model volumes);
  - it blanks `LLM_MODEL` and `EMBEDDING_MODEL`, which the services already read as "no local
    model". So 8-K summaries come from Gemini alone, and search is keyword-only. Form 4 summaries
    use a template and don't change;
  - it lowers a few CPU requests and memory limits.
  `tests/test_k8s_manifests.py` renders production at both sizes and holds each to its budget:
  - **full size:** 9 of 12 GiB and 2 cores, as before;
  - **small size:** 6 GiB minus 768 MiB kept for the OS and Oracle's agent, and 1 core.
  When the VM grows, deleting the component's one line in the overlay brings everything back,
  and the models download again.

Growing is not automatic end to end. The script resizes the VM and says so. Removing the
component is a reviewed pull request, like every other production change.

## Alternatives considered

- **Keep waiting for 2 OCPU and 12 GB only.** Free and simple, but it blocks Phase 7
  indefinitely; three days and about 2,700 attempts brought nothing.
- **A second VM, small now and big later.** Over the allowance while both exist, and it needs a
  data migration between them. Growing in place keeps the disk, the IP, and Atlas's access-list
  entry.
- **Retry the resize blindly, like the create.** A failed create changes nothing, but a resize
  reboots the VM. The capacity report asks first, at no cost.
- **Upgrade the account to pay-as-you-go** for priority capacity. The user declined it on
  2026-10-24, because Oracle places a $100 hold.
- **The fallbacks in brief §11** (serverless, or a spare machine at home). They are a bigger
  change than running the same stack smaller for a while.

## Consequences

- The project can reach production sooner, at the cost of these, while the VM is small:
  - 8-K summaries depend on Gemini's free tier. Without the `gemini` Secret, 8-Ks stay parsed and
    unsummarized.
  - Search is keyword-only.
  - It has one core.
  - The Phase 8 summary benchmark on A1 waits for the full size.
- The small size runs with 768 MiB of headroom at the workloads' limits instead of 3 GiB. That is
  acceptable because limits are ceilings: the workloads' requests total about 1.6 GiB.
- Growing costs one reboot, at a time Oracle chooses. k3s and the data volume, which is mounted
  by UUID from `/etc/fstab`, come back on their own.
- Finishing a new VM is two targeted steps:
  - first the data volume and its attachment;
  - then Atlas's access-list entry.
  A plan run while writing this ADR found that Atlas's API refused the laptop, whose home IP had
  changed since the service account's API access list was set. With one plan for everything,
  that would have left a new VM waiting for its disk. Now it only delays MongoDB access, and the
  log says how to fix it.
- The free-tier allowlist (`infra/terraform/check-free-tier.sh`) gains
  `oci_core_compute_capacity_report`. It creates nothing and costs nothing (ADR-0018).
- Once the VM has grown and the component is gone, the small-size budget test still guards the
  component, in case it's ever needed again.

## Tools in plain language

- **Compute capacity report:** a question to Oracle ("could you place a VM of this size in this
  availability domain right now?"). The answer is `AVAILABLE` or `OUT_OF_HOST_CAPACITY`. It
  creates no resource and costs nothing.
- **Kustomize component:** a reusable bundle of changes, here deletions and settings, that an
  overlay switches on by listing it. Removing the line switches it off.
