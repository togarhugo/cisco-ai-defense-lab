"""LabBot - a minimal chatbot protected by Cisco AI Defense.

Gateway mode (default):
  User -> LabBot -> LiteLLM -> Cisco AI Defense gateway (inline allow/block) -> Claude API
  (route "direct", chosen per message in the UI: LabBot -> LiteLLM -> Claude API, AI Defense bypassed)

API mode (enforce/monitor):
  User -> LabBot --(1) prompt inspection---> Cisco AI Defense Inspection API
                 --(2) if allowed ---------> LiteLLM -> Claude API
                 --(3) response inspection-> Cisco AI Defense Inspection API -> User
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .agent import RUNS, AgentContext, drive, new_run
from .aidefense import AIDefenseClient, MCPInspector
from .config import settings
from .files import SUPPORTED, AttachmentError, build_conversation
from .llm import GatewayClaudeClient
from .mcp_client import MCPToolClient
from .prompts import CATEGORIES, ITEMS
from .verdicts import parse_gateway_block  # noqa: F401  (re-exported)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("labbot")

STATIC = Path(__file__).parent / "static"
BLOCK_PROMPT_MSG = "Your message was blocked by Cisco AI Defense security policy."
BLOCK_RESPONSE_MSG = "The assistant's response was blocked by Cisco AI Defense security policy."
BLOCK_GATEWAY_PROMPT_MSG = "Your message was blocked by the Cisco AI Defense gateway."
BLOCK_GATEWAY_RESPONSE_MSG = "The assistant's response was blocked by the Cisco AI Defense gateway."


class Attachment(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1)          # base64 file content


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=20000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _check(self):
        if self.role != "user" and self.attachments:
            raise ValueError("only user messages can have attachments")
        if not self.content.strip() and not self.attachments:
            raise ValueError("message needs text or at least one file")
        return self


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=50)
    # protected = through Cisco AI Defense; direct = straight to Claude (AI Defense bypassed)
    route: Literal["protected", "direct"] = "protected"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.aid = AIDefenseClient(settings)
    app.state.llm = GatewayClaudeClient(settings)
    app.state.mcp = MCPToolClient(settings.mcp_server_url, settings.agent_tool_timeout)
    app.state.agent = AgentContext(settings, app.state.llm, app.state.mcp, MCPInspector(settings, app.state.aid))
    log.info("agent: %s model=%s mcp=%s mcp_inspection=%s", "on" if settings.agent_enabled else "off",
             settings.agent_model, settings.mcp_server_url, settings.mcp_mode)
    log.info("LabBot %s starting: gateway=%s model=%s aidefense_mode=%s aidefense=%s",
             settings.app_version, settings.llm_base_url, settings.llm_model,
             settings.aidefense_mode, app.state.aid.url)
    yield
    await app.state.aid.aclose()


app = FastAPI(title="LabBot - Cisco AI Defense lab", version=settings.app_version, lifespan=lifespan)
MAX_REQUEST_BYTES = settings.max_total_attachment_bytes * 4 // 3 + 2 * 1024 * 1024   # base64 overhead + text


@app.middleware("http")
async def limit_body(request: Request, call_next):
    """Reject oversized uploads before they are read into memory."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_REQUEST_BYTES:
        return JSONResponse({"detail": f"Request too large (max {MAX_REQUEST_BYTES // (1024 * 1024)} MB)."},
                            status_code=413)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    # never cached: after an upgrade the browser must load the new page, not a stale copy
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/prompts")
async def prompts():
    """The prompt library for the page's dropdowns (test prompts for guardrails, see app/prompts.py)."""
    return {"categories": CATEGORIES, "items": ITEMS}


@app.get("/api/info")
async def info():
    return {"version": settings.app_version, "model": settings.llm_model, "gateway": settings.llm_base_url,
            "model_direct": settings.llm_model_direct,
            "allow_direct": settings.allow_direct_route and settings.aidefense_mode != "off",
            "aidefense_mode": settings.aidefense_mode,
            "inspect_response": settings.aidefense_inspect_response,
            "supported": SUPPORTED,
            "agent": {"enabled": settings.agent_enabled, "model": settings.agent_model,
                      "max_steps": settings.agent_max_steps, "mcp_inspection": settings.mcp_mode,
                      "approval_tools": sorted(settings.approval_tools)},
            "limits": {"max_file_bytes": settings.max_file_bytes,
                       "max_files_per_message": settings.max_files_per_message,
                       "max_total_attachment_bytes": settings.max_total_attachment_bytes}}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    if req.messages[-1].role != "user":
        raise HTTPException(400, "last message must be from the user")

    direct = req.route == "direct"
    if direct and not (settings.allow_direct_route and settings.aidefense_mode != "off"):
        raise HTTPException(403, "The direct route is disabled.")

    rid = str(uuid.uuid4())
    t0 = time.monotonic()
    aid: AIDefenseClient = request.app.state.aid
    llm: GatewayClaudeClient = request.app.state.llm
    try:
        convo, inspect_convo, files = build_conversation(req.messages, settings)
    except AttachmentError as e:
        raise HTTPException(400, str(e))
    model = settings.llm_model_direct if direct else settings.llm_model
    meta = {"src_app": "labbot", "client_transaction_id": rid}
    out = {"request_id": rid, "mode": settings.aidefense_mode, "route": req.route, "blocked": False,
           "blocked_stage": None, "prompt_inspection": None, "response_inspection": None,
           "gateway_error": None, "gateway_verdict": None, "files": files, "reply": None, "model": None}

    # (1) API mode only: inspect the prompt before it reaches Claude (skipped on the direct route)
    if direct:
        p = None
    else:
        p = await aid.inspect("prompt", inspect_convo, meta)
        out["prompt_inspection"] = p.to_dict()
    if p is not None and p.blocked:
        out.update(blocked=True, blocked_stage="prompt", reply=BLOCK_PROMPT_MSG)
        log.info("rid=%s BLOCKED at prompt stage (%.0f ms)", rid, (time.monotonic() - t0) * 1000)
        return out

    # (2) Call Claude
    try:
        r = await llm.chat(convo, model=model)
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
    if settings.aidefense_mode == "gateway" and not direct:
        verdict = parse_gateway_block(r.text)
        if verdict:
            msg = BLOCK_GATEWAY_RESPONSE_MSG if verdict["stage"] == "response" else BLOCK_GATEWAY_PROMPT_MSG
            out.update(blocked=True, blocked_stage=f"gateway-{verdict['stage']}", reply=msg,
                       gateway_verdict=verdict)
            log.info("rid=%s BLOCKED by gateway (%s): %s", rid, verdict["stage"], verdict["rules"])
            return out

    # (3) Inspect Claude's response before it reaches the user
    if settings.aidefense_inspect_response and not r.refused and not direct:
        rs = await aid.inspect("response", inspect_convo + [{"role": "assistant", "content": r.text}], meta)
        out["response_inspection"] = rs.to_dict()
        if rs.blocked:
            out.update(blocked=True, blocked_stage="response", reply=BLOCK_RESPONSE_MSG)
            log.info("rid=%s BLOCKED at response stage (%.0f ms)", rid, (time.monotonic() - t0) * 1000)
            return out

    out["reply"] = r.text
    log.info("rid=%s allowed route=%s files=%d (%.0f ms)", rid, req.route, len(files), (time.monotonic() - t0) * 1000)
    return out


# ---------------------------------------------------------------- agent mode
def _sse_line(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _stream(run, ctx: AgentContext):
    run.busy = True
    try:
        async for ev in drive(run, ctx):
            yield _sse_line(ev)
    except Exception:  # noqa: BLE001 - never leave the browser hanging on a half-open stream
        log.exception("rid=%s agent run crashed", run.run_id)
        RUNS.pop(run.run_id, None)
        yield _sse_line({"type": "error", "message": "The agent run failed unexpectedly. See the LabBot logs."})
    finally:
        run.busy = False


SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


@app.post("/api/agent/run")
async def agent_run(req: ChatRequest, request: Request):
    if not settings.agent_enabled:
        raise HTTPException(404, "Agent mode is disabled.")
    if req.messages[-1].role != "user":
        raise HTTPException(400, "last message must be from the user")
    if req.route == "direct" and not (settings.allow_direct_route and settings.aidefense_mode != "off"):
        raise HTTPException(403, "The direct route is disabled.")
    try:
        convo, _inspect_convo, files = build_conversation(req.messages, settings)
    except AttachmentError as e:
        raise HTTPException(400, str(e))
    route = "direct" if (req.route == "direct" or settings.aidefense_mode == "off") else "protected"
    model = settings.llm_model_direct if route == "direct" else settings.agent_model
    run = new_run(route, model, convo, files)
    return StreamingResponse(_stream(run, request.app.state.agent), media_type="text/event-stream",
                             headers=SSE_HEADERS)


class ApproveRequest(BaseModel):
    run_id: str
    tool_call_id: str
    approve: bool


@app.post("/api/agent/approve")
async def agent_approve(req: ApproveRequest, request: Request):
    run = RUNS.get(req.run_id)
    if run is None:
        raise HTTPException(404, "This run has expired or already finished. Send the request again.")
    if run.busy or run.awaiting != req.tool_call_id:
        raise HTTPException(409, "This run is not waiting for that approval.")
    run.decision = req.approve
    return StreamingResponse(_stream(run, request.app.state.agent), media_type="text/event-stream",
                             headers=SSE_HEADERS)


@app.get("/api/agent/outbox")
async def agent_outbox(request: Request):
    try:
        return {"emails": await request.app.state.mcp.outbox()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Cannot read the mock outbox ({type(e).__name__}).")


@app.delete("/api/agent/outbox")
async def agent_outbox_clear(request: Request):
    try:
        return {"cleared": await request.app.state.mcp.clear_outbox()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Cannot clear the mock outbox ({type(e).__name__}).")
