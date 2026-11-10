"""LLM client: Anthropic Python SDK pointed at the LiteLLM AI gateway.

LiteLLM exposes an Anthropic-compatible /v1/messages endpoint, so the app uses the
official Anthropic SDK with base_url = LiteLLM and api_key = a LiteLLM key.
LiteLLM maps the alias (LLM_MODEL, e.g. "claude-haiku") to the real Claude model.
"""
import logging
from dataclasses import dataclass

import anthropic

from .config import Settings

log = logging.getLogger("llm")


@dataclass
class AgentReply:
    text: str
    blocks: list            # assistant content blocks, to be sent back unchanged on the next step
    tool_uses: list         # [{"type": "tool_use", "id", "name", "input"}]
    stop_reason: str | None
    model: str
    refused: bool = False


@dataclass
class LLMReply:
    text: str
    model: str
    stop_reason: str | None
    refused: bool = False
    raw: dict | None = None  # what the gateway returned: http status, a few headers, the message body


RAW_HEADER_PREFIXES = ("x-litellm-", "x-aidefense", "x-cisco", "request-id", "x-request-id")


class GatewayClaudeClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = anthropic.AsyncAnthropic(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            max_retries=2,
            timeout=settings.llm_timeout,
        )

    async def chat(self, messages: list[dict], model: str | None = None) -> LLMReply:
        """messages: Anthropic format; content is a string or a list of content blocks (text/image)."""
        wire = await self.client.messages.with_raw_response.create(
            model=model or self.s.llm_model,
            max_tokens=self.s.llm_max_tokens,
            system=self.s.system_prompt,
            messages=messages,
        )
        resp = await wire.parse()          # async client: reading the body is awaited (anthropic >= 1)
        raw = None
        try:                               # the raw view is a bonus: never let it break a chat turn
            raw = {"http_status": wire.status_code,
                   "headers": {k: v for k, v in wire.headers.items() if k.lower().startswith(RAW_HEADER_PREFIXES)},
                   "body": resp.model_dump(mode="json", exclude_none=True)}
        except Exception:  # noqa: BLE001
            log.warning("could not capture the raw gateway response", exc_info=True)
        if resp.stop_reason == "refusal":
            log.warning("Claude declined the request")
            return LLMReply("Claude declined to answer this request.", resp.model, resp.stop_reason, refused=True, raw=raw)

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if resp.stop_reason == "max_tokens":
            text += "\n\n[response truncated: max_tokens reached]"
        return LLMReply(text or "(empty response)", resp.model, resp.stop_reason, raw=raw)

    async def agent_step(self, messages: list[dict], tools: list[dict], model: str, system: str,
                         max_tokens: int) -> AgentReply:
        """One agent turn: Claude may answer or ask for tools. Thinking is switched off (the lab shows tool calls,
        and replaying signed thinking blocks through a gateway adds failure modes); if the API refuses that
        setting the request is repeated without it and returned blocks are passed back unchanged."""
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools)
        try:
            resp = await self.client.messages.create(**kwargs, thinking={"type": "disabled"})
        except anthropic.BadRequestError as e:
            if "thinking" not in str(e).lower():
                raise
            log.warning("API refused thinking=disabled, retrying without it: %s", str(e)[:200])
            resp = await self.client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            return AgentReply("Claude declined to continue with this request.", [], [], resp.stop_reason,
                              resp.model, refused=True)
        blocks, tool_uses, text = [], [], []
        for b in resp.content:
            t = getattr(b, "type", None)
            if t == "text":
                blocks.append({"type": "text", "text": b.text})
                text.append(b.text)
            elif t == "tool_use":
                tu = {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                blocks.append(tu)
                tool_uses.append(tu)
            elif t == "thinking":
                blocks.append({"type": "thinking", "thinking": b.thinking, "signature": b.signature})
            elif t == "redacted_thinking":
                blocks.append({"type": "redacted_thinking", "data": b.data})
        return AgentReply("".join(text).strip(), blocks, tool_uses, resp.stop_reason, resp.model)
