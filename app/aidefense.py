"""Client for the Cisco AI Defense Chat Inspection API.

Docs: https://developer.cisco.com/docs/ai-defense-inspection/
The app calls AI Defense twice per turn:
  1. prompt inspection   - conversation up to the latest user message, BEFORE Claude sees it
  2. response inspection - conversation + Claude's answer, BEFORE the user sees it
"""
import logging
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
        if mode == "off":
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
        )
        result.blocked = (mode == "enforce") and not result.is_safe
        log.info("AI Defense %s verdict: safe=%s action=%s classes=%s rules=%s",
                 stage, result.is_safe, result.action, result.classifications, result.rules)
        return result

    async def aclose(self) -> None:
        await self.http.aclose()
