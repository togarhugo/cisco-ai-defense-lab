"""LabBot - a minimal chatbot protected by Cisco AI Defense.

Gateway mode (default):
  User -> LabBot -> LiteLLM -> Cisco AI Defense gateway (inline allow/block) -> Claude API

API mode (enforce/monitor):
  User -> LabBot --(1) prompt inspection---> Cisco AI Defense Inspection API
                 --(2) if allowed ---------> LiteLLM -> Claude API
                 --(3) response inspection-> Cisco AI Defense Inspection API -> User
"""
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .aidefense import AIDefenseClient
from .config import settings
from .llm import GatewayClaudeClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("labbot")

STATIC = Path(__file__).parent / "static"
BLOCK_PROMPT_MSG = "Your message was blocked by Cisco AI Defense security policy."
BLOCK_RESPONSE_MSG = "The assistant's response was blocked by Cisco AI Defense security policy."
BLOCK_GATEWAY_PROMPT_MSG = "Your message was blocked by the Cisco AI Defense gateway."
BLOCK_GATEWAY_RESPONSE_MSG = "The assistant's response was blocked by the Cisco AI Defense gateway."
GATEWAY_BLOCK_RE = re.compile(settings.aidefense_gateway_block_pattern, re.IGNORECASE | re.DOTALL)


def parse_gateway_block(text: str) -> dict | None:
    """Recognise an AI Defense gateway verdict returned as a normal (HTTP 200) message."""
    m = GATEWAY_BLOCK_RE.match(text or "")
    if not m:
        return None
    gd = m.groupdict()
    stage = "response" if (gd.get("stage") or "").lower() == "response" else "prompt"
    rules = [r.strip() for r in (gd.get("rules") or "").split(",") if r.strip()]
    return {"stage": stage, "rules": rules, "verdict": text.strip()}


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=50)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.aid = AIDefenseClient(settings)
    app.state.llm = GatewayClaudeClient(settings)
    log.info("LabBot %s starting: gateway=%s model=%s aidefense_mode=%s aidefense=%s",
             settings.app_version, settings.llm_base_url, settings.llm_model,
             settings.aidefense_mode, app.state.aid.url)
    yield
    await app.state.aid.aclose()


app = FastAPI(title="LabBot - Cisco AI Defense lab", version=settings.app_version, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/info")
async def info():
    return {"version": settings.app_version, "model": settings.llm_model, "gateway": settings.llm_base_url,
            "aidefense_mode": settings.aidefense_mode,
            "inspect_response": settings.aidefense_inspect_response}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    if req.messages[-1].role != "user":
        raise HTTPException(400, "last message must be from the user")

    rid = str(uuid.uuid4())
    t0 = time.monotonic()
    aid: AIDefenseClient = request.app.state.aid
    llm: GatewayClaudeClient = request.app.state.llm
    convo = [m.model_dump() for m in req.messages]
    meta = {"src_app": "labbot", "client_transaction_id": rid}
    out = {"request_id": rid, "mode": settings.aidefense_mode, "blocked": False, "blocked_stage": None,
           "prompt_inspection": None, "response_inspection": None, "gateway_error": None, "gateway_verdict": None,
           "reply": None, "model": None}

    # (1) Inspect the prompt before it reaches Claude
    p = await aid.inspect("prompt", convo, meta)
    out["prompt_inspection"] = p.to_dict()
    if p.blocked:
        out.update(blocked=True, blocked_stage="prompt", reply=BLOCK_PROMPT_MSG)
        log.info("rid=%s BLOCKED at prompt stage (%.0f ms)", rid, (time.monotonic() - t0) * 1000)
        return out

    # (2) Call Claude
    try:
        r = await llm.chat(convo)
    except anthropic.AuthenticationError:
        raise HTTPException(502, "LiteLLM rejected the key - check LLM_API_KEY / LITELLM_MASTER_KEY")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Rate limited by gateway or Claude - try again shortly")
    except anthropic.APIStatusError as e:
        # In gateway mode, HTTP errors are configuration problems (e.g. "No policies are attached
        # to the connection", wrong URL), NOT security blocks - those come back as HTTP 200.
        detail = str(e.message)[:500]
        log.warning("rid=%s gateway/LLM error HTTP %s: %s", rid, e.status_code, detail)
        raise HTTPException(502, f"AI gateway error {e.status_code}: {detail}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Cannot reach the LiteLLM gateway - check the litellm Service/pod")
    out["model"] = r.model

    # (2b) Gateway mode: AI Defense returns its block verdict as the message text
    if settings.aidefense_mode == "gateway":
        verdict = parse_gateway_block(r.text)
        if verdict:
            msg = BLOCK_GATEWAY_RESPONSE_MSG if verdict["stage"] == "response" else BLOCK_GATEWAY_PROMPT_MSG
            out.update(blocked=True, blocked_stage=f"gateway-{verdict['stage']}", reply=msg,
                       gateway_verdict=verdict)
            log.info("rid=%s BLOCKED by gateway (%s): %s", rid, verdict["stage"], verdict["rules"])
            return out

    # (3) Inspect Claude's response before it reaches the user
    if settings.aidefense_inspect_response and not r.refused:
        rs = await aid.inspect("response", convo + [{"role": "assistant", "content": r.text}], meta)
        out["response_inspection"] = rs.to_dict()
        if rs.blocked:
            out.update(blocked=True, blocked_stage="response", reply=BLOCK_RESPONSE_MSG)
            log.info("rid=%s BLOCKED at response stage (%.0f ms)", rid, (time.monotonic() - t0) * 1000)
            return out

    out["reply"] = r.text
    log.info("rid=%s allowed (%.0f ms)", rid, (time.monotonic() - t0) * 1000)
    return out
