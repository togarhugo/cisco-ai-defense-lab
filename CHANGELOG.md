# Changelog

All notable changes to this project. Versions follow [SemVer](https://semver.org): MAJOR.MINOR.PATCH.

## [1.6.5] - 2026-11-05
### Added
- **Markdown in the bot's replies** (Chat and Agent): headings, bold, italic, strike, inline code, fenced code blocks with a
  **Copy** button, bullet and numbered lists (nested), tables, quotes, rules and links.
- Built with DOM nodes only, because model output is untrusted: raw HTML is shown as text, **images are never loaded** (a remote
  image URL can leak data, so `![x](url)` becomes "[image not shown]"), only `http(s)` links are made clickable (new tab,
  `noopener noreferrer`), `javascript:` and `data:` links stay text, and replies over 100,000 characters stay plain text.
  Pattern limits keep abusive input fast.
- `tests/browser/markdown.test.js`: browser test of the renderer (hostile input, speed, normal Markdown). Not part of CI.
### Fixed
- The side panel said "1 LLM calls"; it now pluralises correctly.

## [1.6.4] - 2026-11-05
### Fixed
- A tool call stopped because the AI Defense inspection returned an **error** (fail closed) was shown as "BLOCKED by Cisco AI Defense,
  rules n/a", as if it were a security verdict. It is now shown as "Stopped: AI Defense could not check this", with the error and
  how to continue (`AIDEFENSE_MCP_FAIL_OPEN` or monitor mode). The event carries `fail_closed: true`.
### Added
- Guide: `AIDEFENSE_MCP_FAIL_OPEN: "true"` as the best setting while MCP inspection is not working yet (blocks only on a real unsafe verdict).

## [1.6.3] - 2026-11-04
### Fixed
- `scripts/test-mcp-inspect.sh` said "this is the key or the region" when AI Defense answered `No policies are attached to the
  connection`. It now recognises that answer, prints the **connection id** and the exact steps, and explains that the MCP
  `-32603 Internal Error` is probably the same cause. Messages are no longer cut off.

## [1.6.2] - 2026-11-04
### Changed
- `scripts/test-mcp-inspect.sh` is now a diagnostic: it tests the chat inspection endpoint (proves key and region) and six MCP
  message shapes, prints each result with the request id, and explains the pattern (key/region problem, MCP failing
  inside AI Defense, or only some shapes failing). Never prints the key.
### Added
- Guide: what an MCP `-32603 Internal Error` with a working chat endpoint means and what to check.

## [1.6.1] - 2026-11-04
### Changed
- Default AI Defense inspection host is now **APAC** (`https://ap.api.inspect.aidefense.security.cisco.com`) in the code and in
  `k8s/labbot-config.yaml` (the host from your tenant's API connection example, which resolved from the cluster network).
### Added
- `docs/AGENT_GUIDE.md`: detailed step-by-step guide for Agent mode and MCP (update, API connection and key, test from the
  cluster, optional dedicated agent gateway connection, scenarios, finding events in the dashboard, troubleshooting).

## [1.6.0] - 2026-11-04
### Added
- **Agent mode** (Chat / Agent switch): Claude can use tools through **MCP**; a live trace shows every step, tool call
  and result, with a Mock outbox panel and example prompts.
- **MCP server** `labbot-mcp` (new pod, same image): mock tools (knowledge base with a planted prompt-injection document,
  customer records with fake PII, e-mail, calculator, time) and an optional poisoned-tool-description demo.
- **AI Defense sees the agent and MCP:** each tool call is sent to the AI Defense MCP Inspection API
  (`/api/v1/inspect/mcp`) as the `tools/call` request before it runs and as the result before Claude reads it; the tool list
  is inspected too. The agent's LLM calls go through the gateway on their own alias `claude-agent`
  (secret `AIDEFENSE_AGENT_GATEWAY_URL`, defaults to the chat's URL). Verdicts show the AI Defense `event_id`.
- Human **approval** for `send_email`, step limit, per-call timeout, fail-closed inspection.
- `scripts/test-mcp-inspect.sh` checks the MCP inspection connection from inside the cluster.
- The AI Defense event id is now also kept for chat-API-mode verdicts.
### Changed
- New dependency `mcp` (2.x); `deploy.sh` also restarts and waits for `labbot-mcp`.
- `ensure-secrets.sh` also adds `AIDEFENSE_AGENT_GATEWAY_URL` (copy of the chat gateway URL) when missing.
### Not verified against live AI Defense
- Tested with stand-ins for AI Defense and Claude. Which requests are blocked depends on your policy.

## [1.5.1] - 2026-11-03
### Changed
- Chat page theme: black background (panels, borders and message bubbles adjusted to match).

## [1.5.0] - 2026-11-03
### Added
- **Archives:** `.zip`, `.tar`, `.tar.gz/.tgz`, `.tar.bz2`, `.tar.xz` and single `.gz/.bz2/.xz` files are unpacked in
  memory; Claude gets a manifest (read / skipped and why) plus the contents of every readable file, and up to
  5 pictures from inside as images. AI Defense therefore inspects the text of every file in the archive.
- **Excel (`.xlsx`) and PowerPoint (`.pptx`)** text extraction (no new dependencies).
- Any file type can now be picked in the file dialog (the server decides what it can read).
- Verdict panel lists archive contents counts and each skipped file with the reason.
### Safety
- Archive limits (configurable, see `labbot-config.yaml`): 500 entries, 30 files read, 5 MB per file, 50 MB unpacked,
  200,000 characters, 5 images. Real unpacked bytes are counted (declared sizes are not trusted), nothing is written
  to disk, links are never followed, encrypted entries and nested archives are skipped, corrupt streams are rejected cleanly.
### Fixed
- Drag and drop only worked on the message box; dropping a file anywhere else made the browser open it. Drops
  now work on the **whole page** (with a drop-zone overlay) and **paste works from anywhere**, including copied
  screenshots, which are named `pasted-image-<time>.png`.
- When a file is rejected, the message and attachments are put back in the message box instead of being lost.
- Dropping a folder shows a clear message instead of failing silently.
### Not supported (rejected with a clear message)
- `.7z`, `.rar`, old `.doc/.xls/.ppt`, OpenDocument, HEIC, and binaries such as programs, audio and video.

## [1.4.1] - 2026-11-02
### Fixed
- The chat page is now served with `Cache-Control: no-store`. After an upgrade, browsers could keep showing
  the previous page (no attach button / route selector) while the version badge already said the new version.

## [1.4.0] - 2026-11-02
### Added
- **File and image uploads** in the chat (📎, drag and drop, paste): images (PNG/JPEG/GIF/WebP) go to Claude as
  images; PDF, `.docx` and text/code files are converted to text so AI Defense inspects what Claude reads.
  File types are detected from bytes; limits: 5 files per message, 5 MB each, 20 MB per chat.
- **Route selector** in the chat: *Via AI Defense* (`LLM_MODEL`) or *Direct to Claude* (`LLM_MODEL_DIRECT`,
  AI Defense bypassed), per message. `ALLOW_DIRECT_ROUTE=false` removes the direct option.
- Verdict panel shows the route, the files read (pages, characters, truncation) and a hint when earlier
  direct-route messages cause later blocks.
### Changed
- labbot memory limit 512Mi -> 1Gi for upload bodies; new dependencies `pypdf`, `python-docx`.
- Request bodies above about 27 MB are rejected up front (HTTP 413).
### Fixed
- README: removed a leftover half-sentence in the test-cases section.

## [1.3.1] - 2026-11-02
### Fixed
- README: removed an untested claim that the master key also works as the LiteLLM UI password;
  the UI login is `admin` + the `UI_PASSWORD` secret. Added the steps to reset that password.

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
