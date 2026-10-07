#!/usr/bin/env bash
# First-boot bootstrap of the k3s VM, run once by cloud-init (cloud-init.yaml.tftpl), as root.
# Its output is in /var/log/cloud-init-output.log. Every step checks what is already done, so it
# is safe to run again by hand after a failure:
#   sudo /usr/local/sbin/filings-bootstrap
#
# Settings come from /etc/filings-bootstrap.env, written by cloud-init from Terraform variables.
# Nothing here is secret: the repository is public and k3s generates its own credentials.
set -euo pipefail
# shellcheck source=/dev/null
. /etc/filings-bootstrap.env

log() { echo "filings-bootstrap: $*"; }
die() {
  log "ERROR: $*"
  exit 1
}

# retry <attempts> <command...>, for the steps that download something.
retry() {
  local attempts=$1 n=1
  shift
  until "$@"; do
    [ "$n" -lt "$attempts" ] || return 1
    log "attempt $n of $attempts failed ($1); retrying in 15 s"
    n=$((n + 1))
    sleep 15
  done
}

kubectl() { k3s kubectl "$@"; }

# --- 1. Host firewall ----------------------------------------------------------------------------
# Oracle's Ubuntu images ship iptables rules that reject all inbound and forwarded traffic except
# SSH. The VCN security list already blocks traffic from outside, and those rules stay as a
# second layer, but k3s's pod network (10.42.0.0/16) must get through, or pods can't reach each
# other, CoreDNS, or the API server. Service IPs (10.43.0.0/16) are translated to pod IPs before
# these chains see them. Saved before k3s starts, so k3s's own rules (recreated at every start)
# aren't persisted.
allow() { iptables -C "$@" 2>/dev/null || iptables -I "$@"; }
allow INPUT -s 10.42.0.0/16 -j ACCEPT
allow FORWARD -s 10.42.0.0/16 -j ACCEPT
allow FORWARD -d 10.42.0.0/16 -j ACCEPT
if command -v netfilter-persistent >/dev/null; then
  netfilter-persistent save
fi

# --- 2. Block volume -----------------------------------------------------------------------------
# Terraform attaches the volume after the VM is running, so it may appear a few minutes after this
# script starts. It is the only disk besides the boot disk. A new volume is formatted; a volume
# that already has a filesystem (reattached after the VM was rebuilt) is used as it is.
if ! mountpoint -q "$DATA_MOUNT"; then
  root_disk="/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")"
  data_disk=""
  log "waiting for the block volume"
  for _ in $(seq 1 120); do
    udevadm settle
    data_disk=$(lsblk -dnpo NAME,TYPE | awk -v root="$root_disk" '$2 == "disk" && $1 != root { print $1; exit }')
    [ -z "$data_disk" ] || break
    sleep 10
  done
  [ -n "$data_disk" ] || die "no block volume appeared within 20 minutes"

  if ! blkid "$data_disk" >/dev/null 2>&1; then
    log "formatting $data_disk (a new, empty volume)"
    mkfs.ext4 -q -L k3s-data "$data_disk"
  fi
  uuid=$(blkid -s UUID -o value "$data_disk" || true)
  [ -n "$uuid" ] || die "$data_disk holds something other than a filesystem; not touching it"

  mkdir -p "$DATA_MOUNT"
  if ! grep -q "UUID=$uuid" /etc/fstab; then
    # nofail: the VM still boots without the volume, but k3s won't start (its systemd drop-in
    # requires this mount), so no data can land on the boot disk by mistake.
    echo "UUID=$uuid $DATA_MOUNT ext4 defaults,nofail,x-systemd.device-timeout=5min 0 2" >>/etc/fstab
  fi
  systemctl daemon-reload
  mount "$DATA_MOUNT"
fi
mkdir -p "$DATA_MOUNT/local-path"
log "block volume mounted at $DATA_MOUNT"

# --- 3. k3s --------------------------------------------------------------------------------------
# The install script is fetched from the same release tag as the binary, and it checks the
# binary's SHA-256 against the release's checksum file. Settings: /etc/rancher/k3s/config.yaml.
installed=$(k3s --version 2>/dev/null | awk 'NR == 1 { print $3 }' || true)
if [ "$installed" != "$K3S_VERSION" ]; then
  log "installing k3s $K3S_VERSION"
  retry 5 curl -fsSL -o /tmp/k3s-install.sh \
    "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION/+/%2B}/install.sh"
  retry 3 env INSTALL_K3S_VERSION="$K3S_VERSION" sh /tmp/k3s-install.sh
fi

log "waiting for the Kubernetes API and the node"
for _ in $(seq 1 120); do
  kubectl get --raw /readyz >/dev/null 2>&1 && break
  sleep 5
done
kubectl wait --for=condition=Ready node --all --timeout=10m

# --- 4. Argo CD ----------------------------------------------------------------------------------
# Argo CD's own install (a Kustomization in the repository that pins its version and trims it for
# this node), then the project and Applications. From then on Argo CD syncs everything else,
# including the Sealed Secrets controller and the application, from git.
src=/opt/filings-bootstrap
rm -rf "$src"
git init -q "$src"
git -C "$src" remote add origin "$GIT_REPO_URL"
retry 5 git -C "$src" fetch -q --depth 1 origin "$GIT_REVISION"
git -C "$src" checkout -q FETCH_HEAD
log "applying $ARGOCD_INSTALL_PATH and $ARGOCD_APPS_PATH at $(git -C "$src" rev-parse HEAD)"

# Server-side, because Argo CD's CRDs are too large for client-side apply's annotation.
retry 5 kubectl apply --server-side --force-conflicts -k "$src/$ARGOCD_INSTALL_PATH"
kubectl wait --for=condition=Established --timeout=5m \
  crd/applications.argoproj.io crd/appprojects.argoproj.io
kubectl -n argocd rollout status deployment/argocd-repo-server --timeout=10m
retry 5 kubectl apply -k "$src/$ARGOCD_APPS_PATH"

log "done: Argo CD is syncing the applications in $ARGOCD_APPS_PATH from $GIT_REPO_URL"
