"""Client for the Cisco AI Defense Chat Inspection API (used in API mode only).

Docs: https://developer.cisco.com/docs/ai-defense-inspection/
The app calls AI Defense twice per turn:
  1. prompt inspection   - conversation up to the latest user message, BEFORE Claude sees it
  2. response inspection - conversation + Claude's answer, BEFORE the user sees it
"""
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("aidefense")


@dataclass
class InspectionResult:
    stage: str                      # "prompt" | "response"
    inspected: bool                 # False when mode=off
    is_safe: bool = True
    action: str | None = None       # as returned by AI Defense (e.g. "Allow" / "Block")
    classifications: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    explanation: str | None = None
    error: str | None = None        # set when AI Defense could not be reached / errored
    blocked: bool = False           # final decision taken by this app
    event_id: str | None = None     # AI Defense event id: search for it in the dashboard (Events)
    request_id: str | None = None   # x-aidefense-request-id we sent (MCP inspection)
    severity: str | None = None
    tool: str | None = None         # MCP tool name, when relevant
    raw: dict | None = None         # the JSON body exactly as AI Defense returned it (shown in the UI, collapsed)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AIDefenseClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=settings.aidefense_timeout)

    @property
    def url(self) -> str:
        return self.s.aidefense_base_url.rstrip("/") + self.s.aidefense_chat_path

    async def inspect(self, stage: str, messages: list[dict[str, str]], metadata: dict | None = None) -> InspectionResult:
        mode = self.s.aidefense_mode
        if mode in ("off", "gateway"):
            # gateway mode: inspection happens inline in the AI Defense gateway, not here
            return InspectionResult(stage=stage, inspected=False)

        body: dict[str, Any] = {"messages": messages}
        if metadata:
            body["metadata"] = metadata
        # No "config" block: the policy/rules attached to this API key's connection in the
        # AI Defense dashboard are applied.

        try:
            resp = await self.http.post(
                self.url,
                json=body,
                headers={
                    "X-Cisco-AI-Defense-API-Key": self.s.aidefense_api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            detail = str(exc)
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            log.error("AI Defense %s inspection failed: %s", stage, detail)
            result = InspectionResult(stage=stage, inspected=False, error=detail)
            result.is_safe = self.s.aidefense_fail_open
            result.blocked = (mode == "enforce") and not self.s.aidefense_fail_open
            return result

        result = InspectionResult(
            stage=stage,
            inspected=True,
            is_safe=bool(data.get("is_safe", True)),
            action=data.get("action"),
            classifications=[str(c) for c in (data.get("classifications") or [])],
            rules=[
                str(r.get("rule_name") or r.get("name") or r) if isinstance(r, dict) else str(r)
                for r in (data.get("rules") or [])
            ],
            explanation=data.get("explanation"),
            event_id=data.get("event_id") or None,
            severity=data.get("severity"),
            raw=data if isinstance(data, dict) else None,
        )
        result.blocked = (mode == "enforce") and not result.is_safe
        log.info("AI Defense %s verdict: safe=%s action=%s classes=%s rules=%s",
                 stage, result.is_safe, result.action, result.classifications, result.rules)
        return result

    async def aclose(self) -> None:
        await self.http.aclose()


class MCPInspector:
    """Sends MCP JSON-RPC messages to the AI Defense MCP Inspection API (POST /api/v1/inspect/mcp).

    stages: "mcp-tools-list" (the tool list / descriptions), "mcp-request" (a tools/call before it runs) and
    "mcp-response" (the tool result before the model sees it). Message formats follow the Cisco SDK:
    a response is the original method + params plus a `result`.
    """

    def __init__(self, settings: Settings, aid: AIDefenseClient):
        self.s = settings
        self.http = aid.http

    @property
    def url(self) -> str:
        return self.s.aidefense_base_url.rstrip("/") + "/api/v1/inspect/mcp"

    async def inspect(self, stage: str, message: dict, tool: str | None = None, critical: bool = True) -> InspectionResult:
        """critical=False: an error talking to AI Defense never blocks (used for the tool list)."""
        mode = self.s.mcp_mode
        if mode == "off":
            return InspectionResult(stage=stage, inspected=False, tool=tool)
        request_id = str(uuid.uuid4())
        try:
            resp = await self.http.post(
                self.url, json=message,
                headers={"X-Cisco-AI-Defense-API-Key": self.s.mcp_api_key, "Content-Type": "application/json",
                         "Accept": "application/json", "x-aidefense-request-id": request_id})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                raise ValueError(f"AI Defense error {err.get('code')}: {err.get('message')}")
            result = data.get("result", data) if isinstance(data, dict) else None
            if isinstance(result, dict) and "is_safe" not in result and isinstance(result.get("result"), dict):
                result = result["result"]
            if not isinstance(result, dict) or "is_safe" not in result:
                raise ValueError("unexpected response from the MCP inspection API")
        except (httpx.HTTPError, ValueError) as exc:
            detail = str(exc)
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            log.error("AI Defense %s inspection failed: %s", stage, detail)
            res = InspectionResult(stage=stage, inspected=False, error=detail, request_id=request_id, tool=tool)
            res.is_safe = self.s.aidefense_mcp_fail_open or not critical
            res.blocked = critical and mode == "enforce" and not self.s.aidefense_mcp_fail_open
            return res

        res = InspectionResult(
            stage=stage, inspected=True, is_safe=bool(result.get("is_safe")), action=result.get("action"),
            classifications=[str(c) for c in (result.get("classifications") or [])],
            rules=[str(r.get("rule_name") or r.get("name") or r) if isinstance(r, dict) else str(r)
                   for r in (result.get("rules") or [])],
            explanation=result.get("explanation"), event_id=result.get("event_id") or None,
            severity=result.get("severity"), request_id=request_id, tool=tool, raw=data if isinstance(data, dict) else None)
        res.blocked = mode == "enforce" and not res.is_safe
        log.info("AI Defense %s verdict: safe=%s action=%s rules=%s event=%s tool=%s",
                 stage, res.is_safe, res.action, res.rules, res.event_id, tool)
        return res
