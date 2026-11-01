# Update & versioning workflow

## How versions work

- Every update gets a version `MAJOR.MINOR.PATCH` (e.g. `1.0.0` → `1.1.0`), stored in `VERSION`.
- Each release is a git tag `vX.Y.Z` plus an entry in `CHANGELOG.md`.
- `k8s/kustomization.yaml` pins the image tag to the same version, so **the git version you
  check out = the container version that runs**.
- On every push, GitHub Actions runs the tests and builds `ghcr.io/togarhugo/cisco-ai-defense-lab:<version>`.

```
 You ask for a change ─> Claude commits + tags vX.Y.Z + pushes ─> GitHub Actions builds image :X.Y.Z
                                                                          │
 Ubuntu node:  ./scripts/deploy.sh ── git pull ── kubectl apply -k ───────┘ pulls image :X.Y.Z
```

## Day-to-day: get the newest version on Ubuntu

1. Check the build finished: GitHub → repo → **Actions** → the latest run is green (about 2–3 min after a release).
2. On the Ubuntu node:

```bash
cd ~/cisco-ai-defense-lab && ./scripts/deploy.sh
```

That runs `git pull` → `kubectl apply -k k8s/` → restarts the pods → waits for the rollout → prints the URL.

3. Confirm the version (also shown as a badge in the UI header):

```bash
cat VERSION && kubectl -n ai-defense-lab get deploy labbot -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

## See what changed

```bash
git log --oneline -10
```

```bash
git tag --sort=-v:refname | head
```

```bash
cat CHANGELOG.md
```

## Deploy a specific version / roll back

```bash
./scripts/deploy.sh v1.0.0
```

This checks out that tag and deploys it. To go back to tracking the latest:

```bash
./scripts/deploy.sh
```

## If you edited files on the Ubuntu node

`git pull` refuses to overwrite local edits. Secrets are never in the repo (they live in the
`lab-secrets` Kubernetes Secret), so normally there's nothing to keep. If you did edit a file:

```bash
git stash && ./scripts/deploy.sh && git stash pop
```

Better: ask Claude for the change, so it's versioned in the repo and your node doesn't drift.

## Release process (what Claude runs for each update)

```bash
./scripts/release.sh 1.1.0 "Add PII demo prompts"
```

This bumps `VERSION`, updates the image tag in `k8s/kustomization.yaml`, commits, tags `v1.1.0`,
and pushes `main` plus the tag. `CHANGELOG.md` is updated in the same commit.
