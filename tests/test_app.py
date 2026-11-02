import os

os.environ.update(AIDEFENSE_MODE="enforce", AIDEFENSE_API_KEY="test", LLM_API_KEY="test",
                  LLM_BASE_URL="http://litellm.test:4000",
                  AIDEFENSE_BASE_URL="https://aid.test")

import httpx  # noqa: E402
import respx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.llm import LLMReply  # noqa: E402
from app.main import app  # noqa: E402


class FakeLLM:
    """Stands in for the LiteLLM gateway (the SDK uses httpx2, which respx can't intercept)."""
    def __init__(self):
        self.calls = []
        self.models = []

    async def chat(self, messages, model=None):
        self.calls.append(messages)
        self.models.append(model)
        return LLMReply("Hello from Claude", "claude-opus-5-5", "end_turn")


def client():
    c = TestClient(app)
    c.__enter__()
    app.state.llm = FakeLLM()
    return c

AID = "https://aid.test/api/v1/inspect/chat"
SAFE = {"is_safe": True, "action": "Allow", "classifications": [], "rules": []}
UNSAFE = {"is_safe": False, "action": "Block", "classifications": ["SECURITY_VIOLATION"],
          "rules": [{"rule_name": "Prompt Injection"}]}
BODY = {"messages": [{"role": "user", "content": "hi"}]}


@respx.mock
def test_allowed_flow():
    respx.post(AID).mock(return_value=httpx.Response(200, json=SAFE))
    c = client()
    r = c.post("/api/chat", json=BODY).json()
    llm = app.state.llm
    assert r["blocked"] is False and r["reply"] == "Hello from Claude"
    assert llm.calls == [[{"role": "user", "content": "hi"}]]


@respx.mock
def test_prompt_blocked_never_reaches_llm():
    respx.post(AID).mock(return_value=httpx.Response(200, json=UNSAFE))
    c = client()
    r = c.post("/api/chat", json=BODY).json()
    llm = app.state.llm
    assert r["blocked"] and r["blocked_stage"] == "prompt"
    assert r["prompt_inspection"]["rules"] == ["Prompt Injection"]
    assert llm.calls == []


@respx.mock
def test_response_blocked():
    respx.post(AID).mock(side_effect=[httpx.Response(200, json=SAFE), httpx.Response(200, json=UNSAFE)])
    c = client()
    r = c.post("/api/chat", json=BODY).json()
    assert r["blocked"] and r["blocked_stage"] == "response"
    assert "Hello from Claude" not in r["reply"]


@respx.mock
def test_aidefense_down_fails_closed():
    respx.post(AID).mock(return_value=httpx.Response(503))
    c = client()
    r = c.post("/api/chat", json=BODY).json()
    llm = app.state.llm
    assert r["blocked"] and r["prompt_inspection"]["error"].startswith("HTTP 503")
    assert llm.calls == []


def _gateway_mode(monkeypatch, c):
    import dataclasses
    import app.main as m
    gw = dataclasses.replace(m.settings, aidefense_mode="gateway")
    monkeypatch.setattr(m, "settings", gw)
    app.state.aid.s = gw


@respx.mock
def test_gateway_mode_makes_no_inspection_calls(monkeypatch):
    aid = respx.post(AID).mock(return_value=httpx.Response(200, json=SAFE))
    c = client()
    _gateway_mode(monkeypatch, c)
    r = c.post("/api/chat", json=BODY).json()
    assert r["mode"] == "gateway" and r["blocked"] is False and r["reply"] == "Hello from Claude"
    assert not aid.called


class TextLLM:
    """Gateway stand-in that returns fixed text with HTTP 200 (how AI Defense returns verdicts)."""
    def __init__(self, text):
        self.text = text

    async def chat(self, messages, model=None):
        return LLMReply(self.text, "claude-chat", "end_turn")


def test_gateway_prompt_block_verdict(monkeypatch):
    c = client()
    _gateway_mode(monkeypatch, c)
    app.state.llm = TextLLM("This request violates rules: Prompt Injection")
    r = c.post("/api/chat", json=BODY).json()
    assert r["blocked"] and r["blocked_stage"] == "gateway-prompt"
    assert r["gateway_verdict"]["rules"] == ["Prompt Injection"]
    assert "violates" not in r["reply"]


def test_gateway_response_block_multiple_rules(monkeypatch):
    c = client()
    _gateway_mode(monkeypatch, c)
    app.state.llm = TextLLM("This response violates rules: PII, PCI.")
    r = c.post("/api/chat", json=BODY).json()
    assert r["blocked_stage"] == "gateway-response"
    assert r["gateway_verdict"]["rules"] == ["PII", "PCI"]


def test_normal_answer_mentioning_rules_is_not_blocked(monkeypatch):
    c = client()
    _gateway_mode(monkeypatch, c)
    app.state.llm = TextLLM("Sure. Note: this request violates no rules: it is fine.\nMore text")
    r = c.post("/api/chat", json=BODY).json()
    assert r["blocked"] is False


def test_gateway_http_error_is_config_error_not_block(monkeypatch):
    import anthropic
    import httpx2

    class FailingLLM:
        async def chat(self, messages, model=None):
            resp = httpx2.Response(400, request=httpx2.Request("POST", "http://litellm:4000/v1/messages"))
            raise anthropic.BadRequestError("No policies are attached to the connection", response=resp, body=None)

    c = client()
    _gateway_mode(monkeypatch, c)
    app.state.llm = FailingLLM()
    r = c.post("/api/chat", json=BODY)
    assert r.status_code == 502
    assert "No policies are attached" in r.json()["detail"]
