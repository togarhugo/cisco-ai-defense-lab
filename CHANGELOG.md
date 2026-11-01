# Changelog

All notable changes to this project. Versions follow [SemVer](https://semver.org): MAJOR.MINOR.PATCH.

## [1.3.0] - 2026-11-01
### Added
- **LiteLLM admin UI** at `http://<ubuntu-ip>:30400/ui` (user `admin`), with usage/spend, request logs
  and virtual keys, backed by a new `litellm-db` PostgreSQL pod (data on the node via hostPath, survives reboots).
- `scripts/ensure-secrets.sh`: adds `LITELLM_DB_PASSWORD`, `LITELLM_SALT_KEY` and `UI_PASSWORD` to
  `lab-secrets` only if missing; run automatically by `deploy.sh`. Existing keys are never changed.
- Per-token prices for Haiku 5.5 and Opus 5.5 in the LiteLLM config, so spend in the UI is correct.
### Changed
- LiteLLM image is now `litellm-database:main-stable` (same proxy, with database support).
- LiteLLM rollout never takes the working pod down: `maxUnavailable: 0`, a startup probe for the first
  migrations, and an init container that waits for the database.
- `deploy.sh` waits for the database first, explains failures, and is safe against being replaced by
  `git pull` while running. `release.sh` now only bumps the LabBot image tag.
### Unchanged
- LabBot, model aliases, AI Defense gateway integration and `LITELLM_MASTER_KEY` behaviour.

## [1.2.0] - 2026-11-01
### Added
- LiteLLM aliases `claude-haiku` (Claude Haiku 5.5 via AI Defense gateway) and
  `claude-haiku-direct` (unprotected baseline).
### Changed
- Default model is now `claude-haiku` (cheapest: $0.10 / $0.50 per 1M input/output tokens).
  Switch back to Opus with `LLM_MODEL: "claude-chat"` in `k8s/labbot-config.yaml`.

## [1.1.2] - 2026-11-01
### Fixed
- Gateway mode: AI Defense blocks were shown as a normal chatbot answer. The gateway returns a
  block as an HTTP 200 message (`This request violates rules: Prompt Injection`); LabBot now
  recognises it and shows **BLOCKED (prompt/response)** with the matched rules.
- Gateway HTTP errors (e.g. `No policies are attached to the connection`) are no longer shown as
  security blocks; they appear as `AI gateway error …` (configuration problem).
### Added
- `AIDEFENSE_GATEWAY_BLOCK_PATTERN` setting to adapt to a different verdict wording.

## [1.1.1] - 2026-11-01
### Fixed
- README: wrong example gateway host (`gateway.aidefense.cisco.com`) replaced with the real
  format `<region>.gateway.aidefense.security.cisco.com`; reachability test now reads the host
  from the secret.
- README: repo and image are public, so the private-repo token and pull-secret steps were removed.
### Added
- README: *DNS problems* section (point CoreDNS at a public resolver without changing Ubuntu DNS).
### Changed
- `labbot` Deployment no longer references the `ghcr-pull` pull secret (image is public).

## [1.1.0] - 2026-11-01
### Added
- **AI Defense gateway mode** (new default, `AIDEFENSE_MODE=gateway`):
  LabBot → LiteLLM → Cisco AI Defense gateway → Claude. LiteLLM uses the gateway connection URL
  (`AIDEFENSE_GATEWAY_URL` secret) as the upstream for Claude.
- LiteLLM alias `claude-direct` (bypasses AI Defense) for unprotected baseline comparisons.
- Gateway rejections (HTTP 400/403/406/422/451) are shown in the UI as *BLOCKED (gateway)* with
  the gateway's message.
### Changed
- `AIDEFENSE_API_KEY` is now optional (only used in API mode).
- README rewritten for gateway mode; API mode kept as an alternative.
### Removed
- `claude-fast` LiteLLM alias.

## [1.0.1] - 2026-11-01
### Changed
- GitHub Actions now creates the `vX.Y.Z` git tag automatically after a successful build on `main`
  (tags no longer need to be pushed by hand).

## [1.0.0] - 2026-11-01
### Added
- LabBot chatbot (FastAPI + web UI) with live AI Defense verdict panel.
- Cisco AI Defense Chat Inspection API integration: prompt and response inspection,
  modes `enforce` / `monitor` / `off`, fail-open/fail-closed switch.
- LiteLLM AI gateway between the app and Claude (`claude-chat` -> `claude-opus-5-5`,
  `claude-fast` -> `claude-haiku-5-5`).
- Kubernetes manifests (kustomize) for a single-node cluster, NodePort 30080.
- GitHub Actions image build to GHCR, versioned release and deploy scripts.
