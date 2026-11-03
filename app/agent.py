"""The agent: Claude decides which MCP tools to call, LabBot runs them, and Cisco AI Defense watches both legs.

  LLM leg  : LabBot -> LiteLLM -> AI Defense gateway -> Claude      (every step; blocks come back as 200 text)
  MCP leg  : LabBot -> MCP server (tools)                           (each call is sent to the AI Defense
             MCP Inspection API twice: the tools/call request BEFORE it runs, the result BEFORE Claude sees it)

route "direct" skips AI Defense on both legs (that is the comparison case).

The run is a resumable state machine: `drive()` yields events (for the live trace in the UI) and returns either
when the run is finished or when a human has to approve a tool call (`approval_required`); /api/agent/approve
sets the decision and calls `drive()` again on the same run.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

import anthropic

from .aidefense import InspectionResult, MCPInspector
from .config import Settings
from .llm import GatewayClaudeClient
from .mcp_client import MCPToolClient, ToolError, result_text, to_llm_tools, to_mcp_tool_defs
from .verdicts import parse_gateway_block

log = logging.getLogger("agent")
RUN_TTL_SECONDS = 600
MAX_RUNS = 50
EVENT_STR_LIMIT = 2000


@dataclass
class AgentContext:
    settings: Settings
    llm: GatewayClaudeClient
    mcp: MCPToolClient
    inspector: MCPInspector


@dataclass
class AgentRun:
    run_id: str
    route: str                      # protected | direct
    model: str
    messages: list
    files: list = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    phase: str = "init"             # init | running | awaiting | done
    step: int = 0
    tools: list = field(default_factory=list)
    pending: list = field(default_factory=list)      # tool_use blocks of the last assistant turn not yet run
    results: list = field(default_factory=list)      # tool_result blocks collected for that turn
    awaiting: str | None = None     # tool_use id waiting for a human decision
    decision: bool | None = None
    req_checked: set = field(default_factory=set)
    busy: bool = False
    stats: dict = field(default_factory=lambda: {"llm_calls": 0, "tool_calls": 0, "mcp_inspections": 0, "blocked": 0})

    @property
    def protected(self) -> bool:
        return self.route == "protected"


RUNS: dict[str, AgentRun] = {}


def new_run(route: str, model: str, messages: list, files: list | None = None) -> AgentRun:
    now = time.monotonic()
    for rid in [r for r, x in RUNS.items() if now - x.created > RUN_TTL_SECONDS]:
        RUNS.pop(rid, None)
    while len(RUNS) >= MAX_RUNS:                    # drop the oldest
        RUNS.pop(min(RUNS, key=lambda r: RUNS[r].created), None)
    run = AgentRun(run_id=str(uuid.uuid4()), route=route, model=model, messages=messages, files=files or [])
    RUNS[run.run_id] = run
    return run


def _clip(value):
    """Shorten long strings inside tool arguments for display (the real call is not shortened)."""
    if isinstance(value, str):
        return value if len(value) <= EVENT_STR_LIMIT else value[:EVENT_STR_LIMIT] + "...[shortened for display]"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value]
    return value


def _verdict(v: InspectionResult | None) -> dict | None:
    return v.to_dict() if v is not None else None


def _tool_result_block(call: dict, text: str, is_error: bool) -> dict:
    return {"type": "tool_result", "tool_use_id": call["id"], "content": text, "is_error": is_error}


async def drive(run: AgentRun, ctx: AgentContext) -> AsyncIterator[dict]:
    s, protected = ctx.settings, run.protected

    def finish():
        run.phase = "done"
        RUNS.pop(run.run_id, None)

    def blocked(stage: str, v: InspectionResult | None = None, **extra) -> dict:
        run.stats["blocked"] += 1
        return {"type": "blocked", "stage": stage, "step": run.step, "inspection": _verdict(v),
                # True when AI Defense gave NO verdict (an error) and the call was stopped because inspection fails closed
                "fail_closed": bool(v is not None and v.error),
                "rules": (v.rules if v else extra.pop("rules", [])), "stats": run.stats, **extra}

    # ---------- start: list the MCP tools (and have AI Defense look at their descriptions) ----------
    if run.phase == "init":
        run.phase = "running"
        yield {"type": "run_started", "run_id": run.run_id, "route": run.route, "model": run.model,
               "files": run.files, "mcp_inspection": s.mcp_mode if protected else "bypassed (direct route)"}
        try:
            run.tools = await ctx.mcp.list_tools()
        except ToolError as e:
            yield {"type": "error", "message": f"MCP server unavailable: {e}"}
            finish()
            return
        listing = None
        if protected and s.aidefense_mcp_inspect_tools_list and s.mcp_mode != "off":
            run.stats["mcp_inspections"] += 1
            listing = await ctx.inspector.inspect(
                "mcp-tools-list",
                {"jsonrpc": "2.0", "method": "tools/list", "params": {},
                 "result": {"tools": to_mcp_tool_defs(run.tools)}, "id": f"{run.run_id[:8]}-tools"},
                critical=False)
        yield {"type": "mcp_tools", "tools": [t["name"] for t in run.tools], "inspection": _verdict(listing)}
        if listing is not None and listing.blocked:
            yield blocked("mcp-tools-list", listing, detail="The tool descriptions were flagged, no tool was used.")
            finish()
            return
    run.phase = "running"

    while True:
        # ---------- run the tool calls Claude asked for ----------
        while run.pending:
            call = run.pending[0]
            name, args = call["name"], call.get("input") or {}
            rpc = f"{run.run_id[:8]}-s{run.step}-{call['id'][-6:]}"
            params = {"name": name, "arguments": args}

            if call["id"] not in run.req_checked:
                req = None
                if protected and s.mcp_mode != "off":
                    run.stats["mcp_inspections"] += 1
                    req = await ctx.inspector.inspect(
                        "mcp-request", {"jsonrpc": "2.0", "method": "tools/call", "params": params, "id": rpc}, tool=name)
                run.req_checked.add(call["id"])
                yield {"type": "tool_call", "step": run.step, "id": call["id"], "name": name,
                       "arguments": _clip(args), "needs_approval": name in s.approval_tools, "inspection": _verdict(req)}
                if req is not None and req.blocked:
                    yield blocked("mcp-request", req, tool=name, arguments=_clip(args),
                                  detail="The tool call was stopped before it ran.")
                    finish()
                    return

            if name in s.approval_tools:
                if run.awaiting != call["id"]:
                    run.awaiting, run.decision, run.phase = call["id"], None, "awaiting"
                    yield {"type": "approval_required", "run_id": run.run_id, "tool_call_id": call["id"],
                           "name": name, "arguments": _clip(args)}
                    return
                approved, run.awaiting, run.decision, run.phase = run.decision, None, None, "running"
                if not approved:
                    run.results.append(_tool_result_block(call, "The user denied this action.", True))
                    yield {"type": "tool_result", "step": run.step, "id": call["id"], "name": name, "denied": True,
                           "excerpt": "Denied by the user.", "inspection": None}
                    run.pending.pop(0)
                    continue

            run.stats["tool_calls"] += 1
            try:
                result = await ctx.mcp.call_tool(name, args)
            except ToolError as e:
                result = {"content": [{"type": "text", "text": f"Tool error: {e}"}], "isError": True}

            resp_v = None
            if protected and s.mcp_mode != "off":
                run.stats["mcp_inspections"] += 1
                resp_v = await ctx.inspector.inspect(
                    "mcp-response", {"jsonrpc": "2.0", "method": "tools/call", "params": params, "result": result,
                                     "id": rpc}, tool=name)
                if resp_v.blocked:
                    yield {"type": "tool_result", "step": run.step, "id": call["id"], "name": name,
                           "excerpt": "[result withheld: blocked by AI Defense]", "inspection": _verdict(resp_v)}
                    yield blocked("mcp-response", resp_v, tool=name,
                                  detail="The tool result was withheld from Claude.")
                    finish()
                    return
            text = result_text(result, s.agent_max_tool_result_chars)
            is_error = bool(result.get("isError"))
            run.results.append(_tool_result_block(call, text, is_error))
            yield {"type": "tool_result", "step": run.step, "id": call["id"], "name": name, "is_error": is_error,
                   "chars": len(text), "excerpt": text[:600], "inspection": _verdict(resp_v)}
            run.pending.pop(0)

        if run.results:
            run.messages.append({"role": "user", "content": run.results})
            run.results = []

        # ---------- ask Claude for the next step ----------
        if run.step >= s.agent_max_steps:
            yield {"type": "final", "reply": f"I stopped after {s.agent_max_steps} steps without finishing "
                                             "(step limit). Try a narrower request.", "limit_reached": True,
                   "stats": run.stats}
            finish()
            return
        run.step += 1
        run.stats["llm_calls"] += 1
        yield {"type": "llm_step", "step": run.step}
        try:
            reply = await ctx.llm.agent_step(run.messages, to_llm_tools(run.tools), run.model, s.agent_system_prompt,
                                             s.agent_max_tokens)
        except anthropic.AuthenticationError:
            yield {"type": "error", "message": "LiteLLM rejected the key (check LITELLM_MASTER_KEY / LLM_API_KEY)."}
            finish()
            return
        except anthropic.RateLimitError:
            yield {"type": "error", "message": "Rate limited by the gateway or Claude. Try again shortly."}
            finish()
            return
        except anthropic.APIStatusError as e:
            yield {"type": "error", "message": f"AI gateway error {e.status_code}: {str(e.message)[:400]}"}
            finish()
            return
        except anthropic.APIConnectionError:
            yield {"type": "error", "message": "Cannot reach the LiteLLM gateway."}
            finish()
            return

        if reply.refused:
            yield {"type": "final", "reply": reply.text, "stats": run.stats}
            finish()
            return
        if not reply.tool_uses:
            verdict = parse_gateway_block(reply.text) if protected else None
            if verdict:
                yield blocked(f"gateway-{verdict['stage']}", rules=verdict["rules"], verdict=verdict["verdict"],
                              detail="The AI Defense gateway blocked this LLM call.")
                finish()
                return
            run.messages.append({"role": "assistant", "content": reply.blocks})
            yield {"type": "final", "reply": reply.text or "(no answer)", "stats": run.stats}
            finish()
            return

        run.messages.append({"role": "assistant", "content": reply.blocks})
        run.pending = list(reply.tool_uses)
        yield {"type": "llm_step_done", "step": run.step, "text": reply.text[:600],
               "tools": [t["name"] for t in reply.tool_uses]}
