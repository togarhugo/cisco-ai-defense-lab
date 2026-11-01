#!/usr/bin/env bash
# Maintainer script: bump the version, update the Kubernetes image tag, commit, tag, push.
#   ./scripts/release.sh 1.1.0 "Short description of the change"
set -euo pipefail
cd "$(dirname "$0")/.."
NEW="$1"; MSG="${2:-Release $1}"
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z"; exit 1; }

echo "$NEW" > VERSION
sed -i -E "s/(newTag: \")[^\"]+(\")/\1${NEW}\2/" k8s/kustomization.yaml
git add -A
git commit -m "release: v${NEW} - ${MSG}"
git tag -a "v${NEW}" -m "${MSG}"
git push origin HEAD:main
git push origin "v${NEW}"
