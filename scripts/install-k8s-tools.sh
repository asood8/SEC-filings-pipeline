#!/usr/bin/env bash
# Install the pinned Kubernetes command-line tools (`make k8s-tools`; CI runs it too):
#   kubectl      renders the manifests (`kubectl kustomize`), and later talks to the cluster
#   kubeconform  validates rendered manifests against the Kubernetes API schemas, offline
#   kubeseal     encrypts Secrets into SealedSecrets with the cluster's public key (Phase 7)
#
#   scripts/install-k8s-tools.sh [install-dir]      (default: ~/.local/bin)
#
# A tool already installed at the pinned version is skipped. Downloads go to a fresh temporary
# directory and are checked against the SHA-256 sums pinned below before anything is installed.
# Linux amd64 and arm64 only. See docs/adr/0017-kubernetes-manifests.md.
set -euo pipefail

# kubectl supports clusters one minor version either side; k3s's stable channel is v1.36.
KUBECTL_VERSION=v1.36.5
KUBECONFORM_VERSION=v0.8.0
KUBESEAL_VERSION=0.40.0 # keep in step with deploy/k8s/platform/sealed-secrets

# From dl.k8s.io's .sha256 files and each GitHub release's checksum file.
declare -A SHA256=(
  [kubectl-amd64]=33bc88a24c3b09cf55bfd59e6ca977881b61fc3f25ab65bc80c65b430248648e
  [kubectl-arm64]=88fc1aca8fd0c1b44fe379f67c39bd6858bdfc50241dc55775f84d0f5b31da3a
  [kubeconform-amd64]=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883
  [kubeconform-arm64]=1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87
  [kubeseal-amd64]=9314c35916646e9d59c8f06b1314574b4e79c4d76f079433607ed7b697bb5eb7
  [kubeseal-arm64]=d82f01e9226d194ca7ebc13e3619edefd596b5b433582bcec3d5a8e8b1a03123
)

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) arch=amd64 ;;
  Linux-aarch64 | Linux-arm64) arch=arm64 ;;
  *) echo "unsupported platform: $(uname -s) $(uname -m) (Linux amd64 or arm64 only)" >&2; exit 1 ;;
esac

dest=${1:-$HOME/.local/bin}
mkdir -p "$dest"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# download URL FILE KEY: fetch URL into the work directory and check it against SHA256[KEY].
download() {
  curl -fsSL --retry 3 -o "$work/$2" "$1"
  echo "${SHA256[$3]}  $work/$2" | sha256sum --check --quiet
}

# current TOOL: print the installed version of TOOL in $dest, or nothing.
current() {
  [[ -x "$dest/$1" ]] || return 0
  case $1 in
    kubectl) "$dest/kubectl" version --client 2>/dev/null | sed -n 's/^Client Version: //p' ;;
    kubeconform) "$dest/kubeconform" -v 2>/dev/null ;;
    kubeseal) "$dest/kubeseal" --version 2>/dev/null | sed -n 's/^kubeseal version: //p' ;;
  esac
}

if [[ "$(current kubectl)" == "$KUBECTL_VERSION" ]]; then
  echo "kubectl $KUBECTL_VERSION already installed"
else
  download "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/$arch/kubectl" kubectl "kubectl-$arch"
  install -m 0755 "$work/kubectl" "$dest/kubectl"
  echo "installed kubectl $KUBECTL_VERSION to $dest"
fi

if [[ "$(current kubeconform)" == "$KUBECONFORM_VERSION" ]]; then
  echo "kubeconform $KUBECONFORM_VERSION already installed"
else
  download "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM_VERSION/kubeconform-linux-$arch.tar.gz" \
    kubeconform.tar.gz "kubeconform-$arch"
  tar -xzf "$work/kubeconform.tar.gz" -C "$work" kubeconform
  install -m 0755 "$work/kubeconform" "$dest/kubeconform"
  echo "installed kubeconform $KUBECONFORM_VERSION to $dest"
fi

if [[ "$(current kubeseal)" == "$KUBESEAL_VERSION" ]]; then
  echo "kubeseal $KUBESEAL_VERSION already installed"
else
  download "https://github.com/bitnami/sealed-secrets/releases/download/v$KUBESEAL_VERSION/kubeseal-$KUBESEAL_VERSION-linux-$arch.tar.gz" \
    kubeseal.tar.gz "kubeseal-$arch"
  tar -xzf "$work/kubeseal.tar.gz" -C "$work" kubeseal
  install -m 0755 "$work/kubeseal" "$dest/kubeseal"
  echo "installed kubeseal $KUBESEAL_VERSION to $dest"
fi

case ":$PATH:" in
  *":$dest:"*) ;;
  *) echo "note: $dest is not on PATH" >&2 ;;
esac
