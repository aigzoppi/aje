#!/usr/bin/env bash
# One-shot, idempotent MicroK8s setup for the Autonomic Job Engine (WSL2 Ubuntu or any Ubuntu).
#
#   bash setup/microk8s.sh [--build-worker] [--channel 1.31/stable] [--registry-size 20Gi]
#
# 1. checks systemd (snap needs it; on WSL it is enabled in /etc/wsl.conf, then WSL restarts)
# 2. installs snapd and MicroK8s if missing, waits until the cluster is ready
# 3. enables the addons: dns, hostpath-storage, registry (open registry on port 32000)
# 4. applies deploy/k8s/rbac.yaml (namespace aje-jobs) and writes ~/.kube/microk8s.config
# 5. optionally builds and pushes the worker image (--build-worker)
# 6. prints the registry URL to push to and the AJE settings to use
#
# The registry needs no login: anyone who can reach port 32000 can push and pull.
# Exit codes: 0 ok, 10 WSL must be restarted (systemd was just enabled), other = error.
set -euo pipefail

CHANNEL="${MICROK8S_CHANNEL:-1.31/stable}"
REGISTRY_SIZE="20Gi"
BUILD_WORKER=0
REPO_ROOT="${AJE_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export DEBIAN_FRONTEND=noninteractive

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-worker) BUILD_WORKER=1 ;;
    --channel) CHANNEL="$2"; shift ;;
    --registry-size) REGISTRY_SIZE="$2"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

c_ok=$'\e[32m'; c_info=$'\e[36m'; c_warn=$'\e[33m'; c_off=$'\e[0m'
step() { echo "${c_info}==>${c_off} $*"; }
ok()   { echo "${c_ok}  ✓${c_off} $*"; }
warn() { echo "${c_warn}  !${c_off} $*"; }

is_wsl() { grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; }
mk() { sudo microk8s "$@"; }  # sudo: works before the group membership is active

# ---------------------------------------------------------------- 1. systemd
step "Checking systemd"
if [[ -d /run/systemd/system ]]; then
  ok "systemd is running"
elif is_wsl; then
  warn "systemd is off in this WSL distro: enabling it in /etc/wsl.conf"
  if ! grep -q '^\[boot\]' /etc/wsl.conf 2>/dev/null; then
    printf '\n[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
  elif ! grep -q '^systemd *= *true' /etc/wsl.conf; then
    sudo sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
  fi
  echo
  echo "Restart WSL, then run this script again:"
  echo "   wsl --shutdown        (from Windows; setup.ps1 -RestartWsl does it for you)"
  exit 10
else
  echo "systemd is required by snap/MicroK8s" >&2
  exit 1
fi

# ---------------------------------------------------------------- 2. MicroK8s
step "Checking MicroK8s"
if ! command -v snap >/dev/null 2>&1; then
  step "Installing snapd"
  sudo apt-get -y update
  sudo apt-get -y install snapd
fi
sudo snap wait system seed.loaded
if snap list microk8s >/dev/null 2>&1; then
  ok "MicroK8s installed ($(snap list microk8s | awk 'NR==2 {print $2" "$4}'))"
else
  step "Installing MicroK8s ($CHANNEL)"
  sudo snap install microk8s --classic --channel="$CHANNEL"
fi

if ! id -nG "$USER" | grep -qw microk8s; then
  sudo usermod -a -G microk8s "$USER"
  warn "added $USER to the microk8s group (effective in new shells; this script uses sudo)"
fi
mkdir -p "$HOME/.kube"

if mk status --format short 2>/dev/null | grep -q "microk8s is running"; then
  ok "MicroK8s is running"
else
  # Not `microk8s start`: under sudo its wrapper calls snapctl outside the snap and fails
  # ("cannot invoke snapctl operation commands ... from outside of a snap").
  # snapd starts the services directly; right after install they are usually already booting.
  step "Starting MicroK8s"
  sudo snap start --enable microk8s || warn "snap start failed; waiting anyway"
fi
step "Waiting for the cluster to be ready"
mk status --wait-ready --timeout 600 >/dev/null
ok "cluster ready"

# ---------------------------------------------------------------- 3. addons
addon_enabled() { mk status -a "$1" 2>/dev/null | grep -q '^enabled'; }
enable_addon() {
  local name="$1" arg="${2:-}"
  if addon_enabled "$name"; then
    ok "addon $name already enabled"
  else
    step "Enabling addon $name"
    mk enable "${name}${arg}"
  fi
}
enable_addon dns
enable_addon hostpath-storage
enable_addon registry ":size=${REGISTRY_SIZE}"

step "Waiting for the registry"
mk kubectl -n container-registry rollout status deployment/registry --timeout=300s >/dev/null
for _ in $(seq 1 60); do
  curl -fsS http://localhost:32000/v2/ >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://localhost:32000/v2/ >/dev/null || { echo "registry not reachable on :32000" >&2; exit 1; }
ok "registry answering on http://localhost:32000"

# ---------------------------------------------------------------- 4. engine namespace + kubeconfig
if [[ -f "$REPO_ROOT/deploy/k8s/rbac.yaml" ]]; then
  step "Applying deploy/k8s/rbac.yaml"
  mk kubectl apply -f "$REPO_ROOT/deploy/k8s/rbac.yaml" >/dev/null
  ok "namespace aje-jobs and engine permissions ready"
fi
mk config > "$HOME/.kube/microk8s.config"
chmod 600 "$HOME/.kube/microk8s.config"
ok "kubeconfig written to ~/.kube/microk8s.config"

# ---------------------------------------------------------------- 5. worker image (optional)
IP="$(hostname -I | awk '{print $1}')"
if [[ "$BUILD_WORKER" == 1 ]]; then
  if command -v docker >/dev/null 2>&1; then
    step "Building and pushing the worker image"
    docker build -q -f "$REPO_ROOT/Dockerfile.worker" -t localhost:32000/aje-worker:latest "$REPO_ROOT" >/dev/null
    docker push -q localhost:32000/aje-worker:latest >/dev/null
    ok "pushed localhost:32000/aje-worker:latest"
  else
    warn "docker not found: skipping --build-worker"
  fi
fi

# ---------------------------------------------------------------- 6. summary
cat <<EOF

${c_ok}MicroK8s is ready.${c_off}

Registry (no login, anyone can push and pull):
  from this machine (WSL / Windows):  localhost:32000
  from other machines:                ${IP}:32000   (plain HTTP: add it to Docker's "insecure-registries")

Push an image:
  docker tag my-image localhost:32000/my-image:latest
  docker push localhost:32000/my-image:latest
  curl http://localhost:32000/v2/_catalog          # list repositories

Pods pull it as:  localhost:32000/my-image:latest

Run the engine against this cluster:
  AJE_BACKEND=kubernetes
  AJE_K8S_NAMESPACE=aje-jobs
  AJE_K8S_KUBECONFIG=~/.kube/microk8s.config
  AJE_WORKER_IMAGE=localhost:32000/aje-worker:latest
  AJE_WORKER_AMQP_URL=amqp://aje:aje@${IP}:5672/      # RabbitMQ as seen from the pods

kubectl:  microk8s kubectl get pods -n aje-jobs     (or: export KUBECONFIG=~/.kube/microk8s.config)
EOF
