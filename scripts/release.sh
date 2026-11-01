#!/usr/bin/env bash
# Maintainer script: bump the version, update the Kubernetes image tag, commit, push.
# GitHub Actions then builds image :X.Y.Z and creates the git tag vX.Y.Z automatically.
#   ./scripts/release.sh 1.1.0 "Short description of the change"
set -euo pipefail
cd "$(dirname "$0")/.."
NEW="$1"; MSG="${2:-Release $1}"
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z"; exit 1; }

echo "$NEW" > VERSION
# only the LabBot image tag (the line after its "name:"), not other images such as postgres
sed -i -E '/name: ghcr.io\/togarhugo\/cisco-ai-defense-lab/{n;s/(newTag: ")[^"]+(")/\1'"${NEW}"'\2/}' k8s/kustomization.yaml
git add -A
git commit -m "release: v${NEW} - ${MSG}"
git push origin HEAD:main
