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

    async def chat(self, messages):
        self.calls.append(messages)
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
