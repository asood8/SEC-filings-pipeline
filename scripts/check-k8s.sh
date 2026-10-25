#!/usr/bin/env bash
# Render every Kustomize directory under deploy/ and validate the result, without a cluster
# (`make k8s-check`; CI runs it too). Needs the tools from scripts/install-k8s-tools.sh and a
# network connection: kubeconform downloads JSON schemas (cached in ~/.cache/kubeconform), and
# deploy/argocd/install fetches Argo CD's release manifest.
#
#   1. The vendored Sealed Secrets manifest still matches its upstream checksum.
#   2. `kubectl kustomize` renders each directory.
#   3. kubeconform checks every object against the Kubernetes 1.36 API schemas, strictly
#      (unknown fields are errors). SealedSecret, Application, and AppProject are custom
#      resources; their schemas come from the CRDs-catalog repository, pinned to a commit.
#      CustomResourceDefinitions (only the upstream ones, from Sealed Secrets and Argo CD) have
#      no strict schema, since it can't be generated for them; they get the non-strict one.
#   4. tests/test_k8s_manifests.py checks the project's own rules (resource requests and limits,
#      pod security, pinned images, the memory budget).
#
# See docs/adr/0017-kubernetes-manifests.md.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."

# Kubernetes version of the cluster (k3s stable channel), for the API schemas.
KUBERNETES_VERSION=1.36.5
# Schema sources, pinned to commits so a validation run is reproducible. kubeconform tries them
# in order.
K8S_SCHEMA_REPO=https://raw.githubusercontent.com/yannh/kubernetes-json-schema/8df8a883b68a24a104b4a9e43c1288090ae60b3b
SCHEMAS=(
  -schema-location "$K8S_SCHEMA_REPO/{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json"
  -schema-location "https://raw.githubusercontent.com/datreeio/CRDs-catalog/fd90051867733c60d32d16450556e9cd18459aef/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
  -schema-location "$K8S_SCHEMA_REPO/{{.NormalizedKubernetesVersion}}/{{.ResourceKind}}{{.KindSuffix}}.json"
)
# bitnami/sealed-secrets v0.40.0 controller.yaml (GitHub release asset digest).
SEALED_SECRETS_SHA256=ac8aabccdd9110430d117501705818115b2ab0993700a9c118d4d42cf4015a17

KUSTOMIZATIONS=(
  deploy/k8s/overlays/production
  # Every workload, the local models too: production leaves them out while it lists the
  # small-vm component (docs/adr/0020).
  deploy/k8s/base
  deploy/k8s/platform/sealed-secrets
  deploy/argocd
  deploy/argocd/install
)

echo "${SEALED_SECRETS_SHA256}  deploy/k8s/platform/sealed-secrets/controller.yaml" | sha256sum --check

cache="$HOME/.cache/kubeconform"
mkdir -p "$cache"
kubeconform=(kubeconform -strict -summary -cache "$cache" -kubernetes-version "$KUBERNETES_VERSION"
  "${SCHEMAS[@]}")
for dir in "${KUSTOMIZATIONS[@]}"; do
  echo "== $dir"
  kubectl kustomize "$dir" | "${kubeconform[@]}"
done

# The example Secrets are plain manifests, not a kustomization.
echo "== deploy/k8s/secrets-example"
"${kubeconform[@]}" deploy/k8s/secrets-example

REQUIRE_K8S_TOOLS=1 uv run pytest --quiet tests/test_k8s_manifests.py
