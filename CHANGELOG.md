# Changelog

All notable changes to this project. Versions follow [SemVer](https://semver.org): MAJOR.MINOR.PATCH.

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
