# Changelog

All notable changes to this project. Versions follow [SemVer](https://semver.org): MAJOR.MINOR.PATCH.

## [1.0.0] - 2026-11-01
### Added
- LabBot chatbot (FastAPI + web UI) with live AI Defense verdict panel.
- Cisco AI Defense Chat Inspection API integration: prompt and response inspection,
  modes `enforce` / `monitor` / `off`, fail-open/fail-closed switch.
- LiteLLM AI gateway between the app and Claude (`claude-chat` -> `claude-opus-5-5`,
  `claude-fast` -> `claude-haiku-5-5`).
- Kubernetes manifests (kustomize) for a single-node cluster, NodePort 30080.
- GitHub Actions image build to GHCR, versioned release and deploy scripts.
