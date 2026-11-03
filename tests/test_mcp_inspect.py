import dataclasses
import json

import httpx
import pytest
import respx

import app.main as m
from app.aidefense import AIDefenseClient, MCPInspector

URL = "https://aid.test/api/v1/inspect/mcp"
MSG = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "kb_read", "arguments": {"doc_id": "kb-1"}},
       "id": "r-1"}


def verdict(is_safe=True, rules=(), event="evt-1"):
    return {"jsonrpc": "2.0", "id": "r-1", "result": {
        "is_safe": is_safe, "action": "Allow" if is_safe else "Block", "severity": "NONE_SEVERITY",
        "classifications": [] if is_safe else ["SECURITY_VIOLATION"], "event_id": event,
        "rules": [{"rule_name": r, "rule_id": 0, "entity_types": [""], "classification": "SECURITY_VIOLATION"}
                  for r in rules]}}


def make(**kw):
    s = dataclasses.replace(m.settings, aidefense_base_url="https://aid.test", aidefense_mcp_api_key="mcp-key",
                            aidefense_mcp_mode="enforce", aidefense_mcp_fail_open=False, **kw)
    return MCPInspector(s, AIDefenseClient(s))


@respx.mock
async def test_request_format_headers_and_verdict_parsing():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=verdict(False, ["Prompt Injection"], "evt-42")))
    v = await make().inspect("mcp-request", MSG, tool="kb_read")
    req = route.calls[0].request
    assert json.loads(req.content) == MSG                              # the JSON-RPC message goes as is
    assert req.headers["X-Cisco-AI-Defense-API-Key"] == "mcp-key"
    assert req.headers["x-aidefense-request-id"] == v.request_id and len(v.request_id) == 36
    assert (v.inspected, v.is_safe, v.blocked, v.action) == (True, False, True, "Block")
    assert v.rules == ["Prompt Injection"] and v.classifications == ["SECURITY_VIOLATION"]
    assert v.event_id == "evt-42" and v.tool == "kb_read" and v.severity == "NONE_SEVERITY"


@respx.mock
async def test_safe_verdict_and_doubly_wrapped_result():
    respx.post(URL).mock(return_value=httpx.Response(200, json=verdict(True)))
    v = await make().inspect("mcp-request", MSG)
    assert v.is_safe and not v.blocked and v.event_id == "evt-1"
    wrapped = {"jsonrpc": "2.0", "id": 1, "result": {"result": verdict(False, ["PII"])["result"]}}
    respx.post(URL).mock(return_value=httpx.Response(200, json=wrapped))
    v = await make().inspect("mcp-request", MSG)
    assert v.blocked and v.rules == ["PII"]


@respx.mock
async def test_monitor_mode_reports_but_never_blocks():
    respx.post(URL).mock(return_value=httpx.Response(200, json=verdict(False, ["X"])))
    s = dataclasses.replace(make().s, aidefense_mcp_mode="monitor")
    v = await MCPInspector(s, AIDefenseClient(s)).inspect("mcp-request", MSG)
    assert v.is_safe is False and v.blocked is False


@respx.mock
async def test_off_mode_makes_no_call():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=verdict()))
    s = dataclasses.replace(make().s, aidefense_mcp_mode="off")
    v = await MCPInspector(s, AIDefenseClient(s)).inspect("mcp-request", MSG)
    assert not route.called and v.inspected is False and v.blocked is False


@respx.mock
async def test_errors_fail_closed_by_default_and_open_when_configured():
    respx.post(URL).mock(return_value=httpx.Response(503, text="down"))
    v = await make().inspect("mcp-request", MSG)
    assert v.blocked and v.inspected is False and "HTTP 503" in v.error
    s = dataclasses.replace(make().s, aidefense_mcp_fail_open=True)
    v = await MCPInspector(s, AIDefenseClient(s)).inspect("mcp-request", MSG)
    assert not v.blocked and v.is_safe and v.error


@respx.mock
async def test_non_critical_errors_never_block_the_tool_list():
    respx.post(URL).mock(return_value=httpx.Response(400, text="bad request"))
    v = await make().inspect("mcp-tools-list", MSG, critical=False)
    assert not v.blocked and v.error


@respx.mock
async def test_jsonrpc_error_and_malformed_responses_are_errors():
    for body in ({"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized"}, "id": 1},
                 {"jsonrpc": "2.0", "result": {"unexpected": True}, "id": 1}):
        respx.post(URL).mock(return_value=httpx.Response(200, json=body))
        v = await make().inspect("mcp-request", MSG)
        assert v.error and v.blocked, body


def test_mode_resolution_and_key_fallback():
    base = dataclasses.replace(m.settings, aidefense_api_key="", aidefense_mcp_api_key="")
    assert dataclasses.replace(base).mcp_mode == "off"                                   # auto, no key
    assert dataclasses.replace(base, aidefense_api_key="k").mcp_mode == "enforce"          # auto, chat key reused
    assert dataclasses.replace(base, aidefense_mcp_api_key="k").mcp_mode == "enforce"
    assert dataclasses.replace(base, aidefense_mcp_api_key="k", aidefense_mcp_mode="monitor").mcp_mode == "monitor"
    assert dataclasses.replace(base, aidefense_mcp_mode="bogus").mcp_mode == "off"
    assert dataclasses.replace(base, aidefense_api_key="a", aidefense_mcp_api_key="b").mcp_api_key == "b"
