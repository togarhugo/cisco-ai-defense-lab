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
class LLMReply:
    text: str
    model: str
    stop_reason: str | None
    refused: bool = False


class GatewayClaudeClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = anthropic.AsyncAnthropic(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            max_retries=2,
            timeout=settings.llm_timeout,
        )

    async def chat(self, messages: list[dict[str, str]]) -> LLMReply:
        resp = await self.client.messages.create(
            model=self.s.llm_model,
            max_tokens=self.s.llm_max_tokens,
            system=self.s.system_prompt,
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            log.warning("Claude declined the request")
            return LLMReply("Claude declined to answer this request.", resp.model, resp.stop_reason, refused=True)

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if resp.stop_reason == "max_tokens":
            text += "\n\n[response truncated: max_tokens reached]"
        return LLMReply(text or "(empty response)", resp.model, resp.stop_reason)
