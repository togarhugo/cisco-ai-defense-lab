"""Diagnose the Cisco AI Defense Inspection API connection with the same key, region and network as LabBot.

Runs inside the labbot pod (see test-mcp-inspect.sh). It sends harmless messages only and never prints the key.
"""
import json
import os
import sys
import uuid

import httpx

KEY = os.getenv("AIDEFENSE_MCP_API_KEY") or os.getenv("AIDEFENSE_API_KEY")
BASE = os.getenv("AIDEFENSE_BASE_URL", "").rstrip("/")
if not KEY:
    sys.exit("No API key in the pod: add AIDEFENSE_MCP_API_KEY to the lab-secrets secret, then restart labbot.")
if not BASE:
    sys.exit("AIDEFENSE_BASE_URL is empty.")
print(f"Host: {BASE}\nKey : present ({len(KEY)} characters, not shown)\n")


def call(path, body):
    rid = str(uuid.uuid4())
    try:
        r = httpx.post(BASE + path, json=body, timeout=20, headers={
            "X-Cisco-AI-Defense-API-Key": KEY, "Content-Type": "application/json", "x-aidefense-request-id": rid})
    except httpx.HTTPError as e:
        return False, f"cannot reach AI Defense: {type(e).__name__}: {e}", rid, None
    try:
        data = r.json()
    except ValueError:
        return False, f"HTTP {r.status_code}, not JSON: {r.text[:120]!r}", rid, r.text
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {json.dumps(data)[:160]}", rid, data
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        return False, f"HTTP 200, JSON-RPC error {err.get('code')}: {err.get('message')}", rid, data
    res = data.get("result", data) if isinstance(data, dict) else {}
    if isinstance(res, dict) and "is_safe" not in res and isinstance(res.get("result"), dict):
        res = res["result"]
    if isinstance(res, dict) and "is_safe" in res:
        return True, (f"HTTP 200, is_safe={res.get('is_safe')} action={res.get('action')} "
                      f"event_id={str(res.get('event_id'))[:8]}"), rid, data
    return False, f"HTTP 200 but no verdict in the body: {json.dumps(data)[:160]}", rid, data


TOOLCALL = {"name": "search_documentation", "arguments": {"query": "SSL configuration"}}
TESTS = [
    ("chat  (same key, other endpoint)", "/api/v1/inspect/chat",
     {"messages": [{"role": "user", "content": "Say this is a test!"}]}),
    ("mcp   tools/call, numeric id", "/api/v1/inspect/mcp",
     {"jsonrpc": "2.0", "method": "tools/call", "params": TOOLCALL, "id": 1}),
    ("mcp   tools/call, string id (LabBot)", "/api/v1/inspect/mcp",
     {"jsonrpc": "2.0", "method": "tools/call", "params": TOOLCALL, "id": "labbot-test-1"}),
    ("mcp   tools/call, no arguments", "/api/v1/inspect/mcp",
     {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_current_time", "arguments": {}}, "id": 2}),
    ("mcp   tools/list", "/api/v1/inspect/mcp",
     {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 3}),
    ("mcp   tools/call RESULT", "/api/v1/inspect/mcp",
     {"jsonrpc": "2.0", "method": "tools/call", "params": TOOLCALL, "id": 4,
      "result": {"content": [{"type": "text", "text": "Use TLS 1.2 or newer."}], "isError": False}}),
]
results = {}
for label, path, body in TESTS:
    ok, msg, rid, _ = call(path, body)
    results[label] = ok
    print(f"[{'OK  ' if ok else 'FAIL'}] {label:<40} {msg}")
    if not ok:
        print(f"         request id: {rid}")

chat_ok = results[TESTS[0][0]]
mcp = {k: v for k, v in results.items() if k.startswith("mcp")}
print()
if not chat_ok and not any(mcp.values()):
    print("Everything failed: this is the key or the region, not MCP.\n"
          " - HTTP 401/403: wrong key, or a key from a GATEWAY connection. Use the key of an API connection.\n"
          " - HTTP 404 / cannot reach: wrong AIDEFENSE_BASE_URL (region) or DNS/firewall.")
elif all(mcp.values()):
    print("All MCP message shapes work. LabBot's MCP checks will work: use the Agent page and look for the shield badges.")
elif chat_ok and not any(mcp.values()):
    print("The key and region work (the chat endpoint answered) but EVERY MCP message fails inside AI Defense.\n"
          "That points at the connection or its policy, or at the service, not at LabBot. Check, in order:\n"
          " 1. The connection has an ENABLED policy attached.\n"
          " 2. The policy contains rules that apply to MCP / tool traffic (add some, even a simple one, and retry).\n"
          " 3. The key belongs to an API connection of this tenant and region.\n"
          " 4. If it still fails, ask Cisco support, quoting the 'request id' lines above and the time: they identify the calls.\n"
          "Until it is fixed, set AIDEFENSE_MCP_MODE to \"monitor\" (tools still run, errors only shown) or \"off\" in\n"
          "k8s/labbot-config.yaml. In \"enforce\" mode a failing inspection blocks every tool call (fail closed).")
else:
    good = [k for k, v in mcp.items() if v]
    bad = [k for k, v in mcp.items() if not v]
    print("Some MCP message shapes work and some fail:\n  works: " + "; ".join(good) + "\n  fails: " + "; ".join(bad) +
          "\nSend this whole output to the person maintaining LabBot: the app can be adapted to the shape that works.")
