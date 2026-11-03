"""Recognise an AI Defense gateway verdict that arrives as a normal (HTTP 200) message."""
import re

from .config import settings

GATEWAY_BLOCK_RE = re.compile(settings.aidefense_gateway_block_pattern, re.IGNORECASE | re.DOTALL)


def parse_gateway_block(text: str) -> dict | None:
    m = GATEWAY_BLOCK_RE.match(text or "")
    if not m:
        return None
    gd = m.groupdict()
    stage = "response" if (gd.get("stage") or "").lower() == "response" else "prompt"
    rules = [r.strip() for r in (gd.get("rules") or "").split(",") if r.strip()]
    return {"stage": stage, "rules": rules, "verdict": text.strip()}
