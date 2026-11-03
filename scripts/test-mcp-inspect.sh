#!/usr/bin/env bash
# Checks the connection to the Cisco AI Defense MCP Inspection API from INSIDE the cluster (same network, same key
# as LabBot). Sends one harmless MCP "tools/call" message and prints the verdict and event id.
#   ./scripts/test-mcp-inspect.sh
set -euo pipefail
NS=ai-defense-lab
KUBECTL="${KUBECTL:-kubectl}"
$KUBECTL -n "$NS" exec -i deploy/labbot -- python - <<'PY'
import json, os, sys, uuid
import httpx

key = os.getenv("AIDEFENSE_MCP_API_KEY") or os.getenv("AIDEFENSE_API_KEY")
base = os.getenv("AIDEFENSE_BASE_URL", "").rstrip("/")
if not key:
    sys.exit("No API key: add AIDEFENSE_MCP_API_KEY to the lab-secrets secret (README, Agent mode, step 2).")
url = base + "/api/v1/inspect/mcp"
msg = {"jsonrpc": "2.0", "method": "tools/call", "id": "labbot-test-1",
       "params": {"name": "calculator", "arguments": {"expression": "2+2"}}}
print("POST", url)
try:
    r = httpx.post(url, json=msg, timeout=15, headers={
        "X-Cisco-AI-Defense-API-Key": key, "Content-Type": "application/json",
        "x-aidefense-request-id": str(uuid.uuid4())})
except httpx.HTTPError as e:
    sys.exit(f"Could not reach AI Defense: {type(e).__name__}: {e}\nCheck AIDEFENSE_BASE_URL (your region) and DNS/firewall.")
print("HTTP", r.status_code)
print(json.dumps(r.json(), indent=2)[:1500] if r.headers.get("content-type", "").startswith("application/json") else r.text[:500])
if r.status_code in (401, 403):
    print("-> the key was rejected: use the API key of an API connection (not the gateway connection).")
elif r.status_code == 404:
    print("-> wrong path or region: set AIDEFENSE_BASE_URL in k8s/labbot-config.yaml to your region.")
PY
