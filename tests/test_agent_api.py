import base64
import dataclasses
import json

import httpx
import respx
from fastapi.testclient import TestClient

import app.main as m
from app import agent as A
from app.aidefense import AIDefenseClient, MCPInspector
from tests.test_agent import FakeMCP, ScriptedLLM, ai_defense, calls, final, tu


BASE_SETTINGS = m.settings      # captured at import: every client starts from the same defaults


def client(monkeypatch, llm, mcp, **overrides):
    A.RUNS.clear()
    c = TestClient(m.app)
    c.__enter__()
    opts = dict(aidefense_base_url="https://aid.test", aidefense_mcp_api_key="k", aidefense_mcp_mode="enforce")
    opts.update(overrides)
    s = dataclasses.replace(BASE_SETTINGS, **opts)
    monkeypatch.setattr(m, "settings", s)
    m.app.state.agent = A.AgentContext(s, llm, mcp, MCPInspector(s, AIDefenseClient(s)))
    m.app.state.mcp = mcp
    return c


def sse(response):
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def post_run(c, text="go", route="protected", files=()):
    body = {"messages": [{"role": "user", "content": text, "attachments": list(files)}], "route": route}
    with c.stream("POST", "/api/agent/run", json=body) as r:
        return r.status_code, (sse(r) if r.status_code == 200 else r.read() and r.json()), r.headers


@respx.mock
def test_run_streams_events_as_server_sent_events(monkeypatch):
    ai_defense()
    c = client(monkeypatch, ScriptedLLM(calls(tu(1, "kb_search", query="q")), final("All done.")), FakeMCP())
    status, events, headers = post_run(c)
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-store"
    assert [e["type"] for e in events][0] == "run_started" and events[-1] == {
        "type": "final", "reply": "All done.", "stats": {"llm_calls": 2, "tool_calls": 1, "mcp_inspections": 3, "blocked": 0}}
    assert events[0]["route"] == "protected" and events[0]["model"] == "claude-agent"


@respx.mock
def test_approval_round_trip_over_http(monkeypatch):
    ai_defense()
    mcp = FakeMCP()
    llm = ScriptedLLM(calls(tu(1, "send_email", to="a@b.example", subject="s", body="b")), final("Sent."))
    c = client(monkeypatch, llm, mcp)
    _, events, _ = post_run(c)
    ask = events[-1]
    assert ask["type"] == "approval_required" and mcp.calls == []

    assert c.post("/api/agent/approve", json={"run_id": "nope", "tool_call_id": "x", "approve": True}).status_code == 404
    assert c.post("/api/agent/approve", json={"run_id": ask["run_id"], "tool_call_id": "wrong",
                                              "approve": True}).status_code == 409
    with c.stream("POST", "/api/agent/approve",
                  json={"run_id": ask["run_id"], "tool_call_id": ask["tool_call_id"], "approve": True}) as r:
        rest = sse(r)
    assert rest[-1]["type"] == "final" and mcp.calls[0][0] == "send_email"
    # the decision cannot be replayed once the run has finished
    assert c.post("/api/agent/approve", json={"run_id": ask["run_id"], "tool_call_id": ask["tool_call_id"],
                                              "approve": True}).status_code == 404


@respx.mock
def test_block_is_reported_as_an_event_not_an_http_error(monkeypatch):
    ai_defense(lambda b: "Data Exfiltration" if "external-review.example" in json.dumps(b) else None)
    llm = ScriptedLLM(calls(tu(1, "send_email", to="x@external-review.example", subject="s", body="b")))
    c = client(monkeypatch, llm, FakeMCP())
    status, events, _ = post_run(c)
    assert status == 200 and events[-1]["type"] == "blocked" and events[-1]["rules"] == ["Data Exfiltration"]


@respx.mock
def test_direct_route_and_its_guards(monkeypatch):
    route = respx.post("https://aid.test/api/v1/inspect/mcp").mock(return_value=httpx.Response(200, json={}))
    c = client(monkeypatch, ScriptedLLM(final("hi")), FakeMCP())
    status, events, _ = post_run(c, route="direct")
    assert status == 200 and events[0]["route"] == "direct" and events[0]["model"] == "claude-haiku-direct"
    assert not route.called

    c = client(monkeypatch, ScriptedLLM(), FakeMCP(), allow_direct_route=False)
    assert post_run(c, route="direct")[0] == 403
    c = client(monkeypatch, ScriptedLLM(final("x")), FakeMCP(), aidefense_mode="off")          # AI Defense off: never "protected"
    assert post_run(c, route="protected")[1][0]["route"] == "direct"


def test_disabled_agent_and_bad_requests(monkeypatch):
    c = client(monkeypatch, ScriptedLLM(), FakeMCP(), agent_enabled=False)
    assert post_run(c)[0] == 404
    c = client(monkeypatch, ScriptedLLM(), FakeMCP())
    bad = {"messages": [{"role": "assistant", "content": "x"}]}
    assert c.post("/api/agent/run", json=bad).status_code == 400
    status, body, _ = post_run(c, files=[{"name": "a.exe", "data": base64.b64encode(b"MZ\x00" * 50).decode()}])
    assert status == 400 and "not a supported file" in body["detail"]


@respx.mock
def test_attached_files_reach_the_agent_and_are_summarised(monkeypatch):
    ai_defense()
    llm = ScriptedLLM(final("It says hello."))
    c = client(monkeypatch, llm, FakeMCP())
    f = {"name": "n.txt", "data": base64.b64encode(b"hello from a file").decode()}
    _, events, _ = post_run(c, "what does it say?", files=[f])
    assert events[0]["files"][0]["name"] == "n.txt" and events[0]["files"][0]["kind"] == "text"
    first = llm.seen[0][0]["content"]
    assert "hello from a file" in first[0]["text"] and first[-1]["text"] == "what does it say?"


def test_outbox_proxy_and_info(monkeypatch):
    class Box(FakeMCP):
        async def outbox(self):
            return [{"id": 1, "to": "a@b.example", "subject": "s", "body": "b", "time": "t"}]

        async def clear_outbox(self):
            return 1

    c = client(monkeypatch, ScriptedLLM(), Box())
    assert c.get("/api/agent/outbox").json()["emails"][0]["to"] == "a@b.example"
    assert c.delete("/api/agent/outbox").json() == {"cleared": 1}
    info = c.get("/api/info").json()["agent"]
    assert info == {"enabled": True, "model": "claude-agent", "max_steps": 8, "mcp_inspection": "enforce",
                    "approval_tools": ["send_email"]}

    class Down(FakeMCP):
        async def outbox(self):
            raise ConnectionError("refused")

    c = client(monkeypatch, ScriptedLLM(), Down())
    assert c.get("/api/agent/outbox").status_code == 502
