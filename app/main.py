"""LabBot - a minimal chatbot with Cisco AI Defense in front of the AI gateway.

  User -> LabBot --(1) prompt inspection---> Cisco AI Defense (allow/block)
                 --(2) if allowed ---------> LiteLLM gateway --> Claude API
                 --(3) response inspection-> Cisco AI Defense (allow/block) -> User
"""
import logging
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
           "prompt_inspection": None, "response_inspection": None, "reply": None, "model": None}

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
        raise HTTPException(502, f"AI gateway error {e.status_code}: {str(e.message)[:300]}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Cannot reach the LiteLLM gateway - check the litellm Service/pod")
    out["model"] = r.model

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
