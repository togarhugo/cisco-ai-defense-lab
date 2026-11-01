#!/usr/bin/env bash
# Run on the Ubuntu Kubernetes node: pull the latest version from git and roll it out.
#   ./scripts/deploy.sh            -> latest main
#   ./scripts/deploy.sh v1.1.0     -> a specific released version (also used to roll back)
set -euo pipefail
cd "$(dirname "$0")/.."
NS=ai-defense-lab
KUBECTL="${KUBECTL:-kubectl}"

git fetch --tags origin
if [[ $# -ge 1 ]]; then
  git checkout -q "$1"
else
  git checkout -q main
  git pull --ff-only origin main
fi
echo ">> Deploying version $(cat VERSION) ($(git rev-parse --short HEAD))"

$KUBECTL apply -k k8s/
# ConfigMaps don't restart pods by themselves - restart so config changes take effect
$KUBECTL -n "$NS" rollout restart deploy/litellm deploy/labbot
$KUBECTL -n "$NS" rollout status deploy/litellm --timeout=180s
$KUBECTL -n "$NS" rollout status deploy/labbot --timeout=180s
$KUBECTL -n "$NS" get pods -o wide
IP=$(hostname -I | awk '{print $1}')
echo ">> LabBot: http://${IP}:30080"
