#!/usr/bin/env bash
# Run on the Ubuntu Kubernetes node: pull the latest version from git and roll it out.
#   ./scripts/deploy.sh            -> latest main
#   ./scripts/deploy.sh v1.2.0     -> a specific released version (also used to roll back)
#
# Everything is inside main() on purpose: bash reads the whole function before running it, so
# `git pull` replacing this very file mid-run cannot corrupt the execution.
main() {
  set -euo pipefail
  cd "$(dirname "$0")/.."
  local NS=ai-defense-lab
  local KUBECTL="${KUBECTL:-kubectl}"

  git fetch --tags origin
  if [[ $# -ge 1 ]]; then
    git checkout -q "$1"
  else
    git checkout -q main
    git pull --ff-only origin main
  fi
  echo ">> Deploying version $(cat VERSION) ($(git rev-parse --short HEAD))"

  # secrets for the LiteLLM UI/database (only adds what is missing)
  ./scripts/ensure-secrets.sh

  $KUBECTL apply -k k8s/

  # database first (LiteLLM waits for it)
  if $KUBECTL -n "$NS" get deploy/litellm-db >/dev/null 2>&1; then
    if ! $KUBECTL -n "$NS" rollout status deploy/litellm-db --timeout=240s; then
      echo "!! litellm-db is not ready. The existing litellm/labbot pods keep serving traffic." >&2
      echo "   Check: $KUBECTL -n $NS describe pod -l app=litellm-db | tail -20" >&2
      echo "   ImagePullBackOff = Docker Hub blocked: see the comment in k8s/kustomization.yaml" >&2
      return 1
    fi
  fi

  # ConfigMaps don't restart pods by themselves - restart so config changes take effect
  $KUBECTL -n "$NS" rollout restart deploy/litellm deploy/labbot
  if ! $KUBECTL -n "$NS" rollout status deploy/litellm --timeout=360s; then
    echo "!! litellm did not become ready. The previous litellm pod keeps serving, so the chatbot still works." >&2
    echo "   Check: $KUBECTL -n $NS logs deploy/litellm --tail=40" >&2
    echo "   Roll back the manifests: ./scripts/deploy.sh v1.2.0" >&2
    return 1
  fi
  $KUBECTL -n "$NS" rollout status deploy/labbot --timeout=180s
  $KUBECTL -n "$NS" get pods -o wide
  local IP
  IP=$(hostname -I | awk '{print $1}')
  echo ">> LabBot:        http://${IP}:30080"
  echo ">> LiteLLM UI:    http://${IP}:30400/ui   (user: admin)"
}
main "$@"; exit $?
