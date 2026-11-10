"""The raw AI Defense response and the timing that the side-by-side view shows."""
import os

os.environ.update(AIDEFENSE_MODE="enforce", AIDEFENSE_API_KEY="test", LLM_API_KEY="test",
                  LLM_BASE_URL="http://litellm.test:4000", AIDEFENSE_BASE_URL="https://aid.test")

import anthropic  # noqa: E402
import httpx  # noqa: E402
import respx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.llm import GatewayClaudeClient, LLMReply  # noqa: E402
from app.main import app  # noqa: E402

AID = "https://aid.test/api/v1/inspect/chat"
SAFE = {"is_safe": True, "action": "Allow", "classifications": [], "rules": [], "event_id": "evt-1"}
UNSAFE = {"is_safe": False, "action": "Block", "classifications": ["SECURITY_VIOLATION"],
          "rules": [{"rule_name": "Prompt Injection"}], "event_id": "evt-2"}
BODY = {"messages": [{"role": "user", "content": "hi"}]}
GW_RAW = {"http_status": 200, "headers": {"x-litellm-call-id": "abc"},
          "body": {"id": "msg_1", "content": [{"type": "text", "text": "Hello"}]}}


class FakeLLM:
    def __init__(self, text="Hello from Claude", raw=GW_RAW):
        self.text, self.raw = text, raw

    async def chat(self, messages, model=None):
        return LLMReply(self.text, "claude-x", "end_turn", raw=self.raw)


def client(llm=None):
    c = TestClient(app)
    c.__enter__()
    app.state.llm = llm or FakeLLM()
    return c


def gateway(monkeypatch):
    import dataclasses
    import app.main as m
    gw = dataclasses.replace(m.settings, aidefense_mode="gateway")
    monkeypatch.setattr(m, "settings", gw)
    app.state.aid.s = gw


@respx.mock
def test_api_mode_returns_the_raw_inspection_json():
    respx.post(AID).mock(side_effect=[httpx.Response(200, json=SAFE), httpx.Response(200, json=SAFE)])
    r = client().post("/api/chat", json=BODY).json()
    assert r["aidefense_raw"] == {"source": "inspection-api", "prompt": SAFE, "response": SAFE | {}}
    assert isinstance(r["latency_ms"], int) and r["latency_ms"] >= 0


@respx.mock
def test_api_mode_prompt_block_keeps_the_raw_verdict():
    respx.post(AID).mock(return_value=httpx.Response(200, json=UNSAFE))
    r = client().post("/api/chat", json=BODY).json()
    assert r["blocked"] and r["aidefense_raw"]["prompt"] == UNSAFE and r["aidefense_raw"]["source"] == "inspection-api"
    assert r["latency_ms"] is not None


def test_gateway_mode_returns_what_the_gateway_sent(monkeypatch):
    c = client()
    gateway(monkeypatch)
    r = c.post("/api/chat", json=BODY).json()
    assert r["aidefense_raw"] == {"source": "gateway", **GW_RAW}


def test_gateway_block_keeps_the_raw_verdict_text(monkeypatch):
    block = {"http_status": 200, "headers": {}, "body": {"content": [{"type": "text", "text": "This request violates rules: PII"}]}}
    c = client(FakeLLM("This request violates rules: PII", block))
    gateway(monkeypatch)
    r = c.post("/api/chat", json=BODY).json()
    assert r["blocked"] and r["aidefense_raw"]["body"]["content"][0]["text"].startswith("This request violates")


@respx.mock
def test_direct_route_has_no_aidefense_raw():
    aid = respx.post(AID).mock(return_value=httpx.Response(200, json=SAFE))
    r = client().post("/api/chat", json=BODY | {"route": "direct"}).json()
    assert r["aidefense_raw"] is None and r["reply"] == "Hello from Claude" and not aid.called
    assert r["latency_ms"] is not None


class _Wire:
    status_code = 200
    headers = {"X-LiteLLM-Call-Id": "c1", "x-litellm-response-cost": "0.001", "set-cookie": "secret=1",
               "authorization": "Bearer nope", "request-id": "req_1"}

    def parse(self):
        return anthropic.types.Message(id="msg_1", type="message", role="assistant", model="claude-x", stop_reason="end_turn",
                                       content=[{"type": "text", "text": "Hi there"}],
                                       usage={"input_tokens": 3, "output_tokens": 2})


async def test_client_keeps_only_harmless_headers_and_the_body():
    c = GatewayClaudeClient(settings)

    class Messages:
        class with_raw_response:
            @staticmethod
            async def create(**kw):
                return _Wire()

    c.client = type("C", (), {"messages": Messages})()
    r = await c.chat([{"role": "user", "content": "x"}])
    assert r.text == "Hi there" and r.raw["http_status"] == 200
    assert set(r.raw["headers"]) == {"X-LiteLLM-Call-Id", "x-litellm-response-cost", "request-id"}
    assert r.raw["body"]["content"][0]["text"] == "Hi there" and r.raw["body"]["usage"]["output_tokens"] == 2
