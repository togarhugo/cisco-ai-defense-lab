"""LabBot as an MCP client (Streamable HTTP). One short session per operation, so nothing is held open
while the agent waits for a human approval."""
import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("mcp-client")


class ToolError(Exception):
    """The MCP server could not be reached or the call failed at the protocol level."""


class MCPToolClient:
    def __init__(self, url: str, timeout: float = 20.0):
        self.url = url
        self.timeout = timeout

    async def list_tools(self) -> list[dict]:
        """MCP tools as [{"name", "description", "input_schema"}]."""
        from mcp import Client
        try:
            async with Client(self.url) as c:
                res = await asyncio.wait_for(c.list_tools(), self.timeout)
        except Exception as e:  # noqa: BLE001 - network, protocol, timeout: all mean "tools unavailable"
            raise ToolError(f"cannot list tools on the MCP server ({type(e).__name__}: {e})") from None
        return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in res.tools]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Result in MCP form: {"content": [...], "isError": bool}."""
        from mcp import Client
        try:
            async with Client(self.url) as c:
                res = await asyncio.wait_for(c.call_tool(name, arguments), self.timeout)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"tool call failed ({type(e).__name__}: {e})") from None
        return {"content": [x.model_dump(exclude_none=True) for x in res.content], "isError": bool(res.is_error)}

    # --- lab helper: the mock outbox is a plain HTTP route next to /mcp, not an MCP tool ---
    def _base(self) -> str:
        u = urlsplit(self.url)
        return urlunsplit((u.scheme, u.netloc, "", "", ""))

    async def outbox(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=5) as h:
            r = await h.get(self._base() + "/outbox")
            r.raise_for_status()
            return r.json().get("emails", [])

    async def clear_outbox(self) -> int:
        async with httpx.AsyncClient(timeout=5) as h:
            r = await h.delete(self._base() + "/outbox")
            r.raise_for_status()
            return r.json().get("cleared", 0)


def to_mcp_tool_defs(tools: list[dict]) -> list[dict]:
    """Tool list in MCP wire format (for AI Defense inspection of tools/list)."""
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in tools]


def to_llm_tools(tools: list[dict]) -> list[dict]:
    """MCP tools -> Anthropic Messages API tool definitions."""
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools]


def result_text(result: dict, limit: int) -> str:
    parts = []
    for c in result.get("content") or []:
        parts.append(c.get("text", "") if c.get("type") == "text" else f"[{c.get('type', 'content')}]")
    text = "\n".join(parts).strip() or "(empty result)"
    return text if len(text) <= limit else text[:limit] + "\n[... result truncated ...]"
