#!/usr/bin/env bash
# Fallback: build the LabBot image ON the Ubuntu node and load it into the cluster's
# container runtime, instead of pulling from GHCR. Needs docker installed.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(cat VERSION)
IMG=ghcr.io/togarhugo/cisco-ai-defense-lab:${VERSION}

docker build --build-arg APP_VERSION="$VERSION" -t "$IMG" .

if command -v k3s >/dev/null; then
  docker save "$IMG" | sudo k3s ctr images import -
elif command -v microk8s >/dev/null; then
  docker save "$IMG" | microk8s ctr image import -
elif command -v kind >/dev/null; then
  kind load docker-image "$IMG"
elif command -v minikube >/dev/null; then
  minikube image load "$IMG"
elif command -v ctr >/dev/null; then   # kubeadm + containerd
  docker save "$IMG" | sudo ctr -n k8s.io images import -
else
  echo "Unknown runtime: import $IMG into your cluster manually"; exit 1
fi
echo ">> Loaded $IMG into the cluster runtime. Now run: ./scripts/deploy.sh"
