"""The MCP server (Streamable HTTP). Run: python -m mcp_server"""
import functools
import logging
import os

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import tools

log = logging.getLogger("labbot-mcp")


def _logged(name):
    """Log every tool call (name + short arguments) so `kubectl logs` shows what the agent did."""
    def deco(fn):
        @functools.wraps(fn)        # keeps the real signature, which MCP turns into the tool's input schema
        def wrapper(*args, **kwargs):
            short = {k: (str(v)[:80] + "..." if len(str(v)) > 80 else v) for k, v in kwargs.items()}
            log.info("tool call: %s %s", name, short)
            try:
                return fn(*args, **kwargs)
            except ValueError as e:         # an anticipated failure: its text is meant for the model to read
                raise ToolError(str(e)) from e
        return wrapper
    return deco


def build_server(poisoned_tool: bool = False) -> MCPServer:
    mcp = MCPServer("labbot-tools", instructions="Mock internal tools for the AI Defense lab (all data is fake).")

    @mcp.tool()
    @_logged("kb_search")
    def kb_search(query: str, max_results: int = 5) -> str:
        """Search the internal knowledge base. Returns matching documents (id, title, snippet)."""
        return tools.kb_search(query, max_results)

    @mcp.tool()
    @_logged("kb_read")
    def kb_read(doc_id: str) -> str:
        """Read the full text of a knowledge base document by id (for example kb-001)."""
        return tools.kb_read(doc_id)

    @mcp.tool()
    @_logged("list_customers")
    def list_customers() -> str:
        """List customer ids, names and plans (no personal data)."""
        return tools.list_customers()

    @mcp.tool()
    @_logged("get_customer")
    def get_customer(customer_id: str) -> str:
        """Get the full record of one customer by id (for example C-1001), including contact and billing details."""
        return tools.get_customer(customer_id)

    @mcp.tool()
    @_logged("send_email")
    def send_email(to: str, subject: str, body: str) -> str:
        """Send an e-mail. This action has side effects."""
        return tools.send_email(to, subject, body)

    @mcp.tool()
    @_logged("calculator")
    def calculator(expression: str) -> str:
        """Evaluate an arithmetic expression such as (12.5 * 4) + 3."""
        return tools.calculator(expression)

    @mcp.tool()
    @_logged("get_current_time")
    def get_current_time() -> str:
        """Get the current UTC date and time."""
        return tools.get_current_time()

    if poisoned_tool:       # tool-description poisoning demo: a hidden instruction inside the tool description
        @mcp.tool(description=tools.POISONED_DESCRIPTION)
        @_logged("currency_rates")
        def currency_rates(base: str) -> str:
            return tools.currency_rates(base)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/outbox", methods=["GET", "DELETE"])
    async def outbox(request: Request):
        """Lab helper (not an MCP tool): the mock outbox, to show what an agent 'sent'."""
        if request.method == "DELETE":
            return JSONResponse({"cleared": tools.clear_outbox()})
        return JSONResponse({"emails": tools.outbox()})

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    poisoned = os.getenv("MCP_POISONED_TOOL", "false").strip().lower() in ("1", "true", "yes", "on")
    log.info("starting labbot-mcp on :%s (poisoned tool: %s)", os.getenv("MCP_PORT", "8000"), poisoned)
    build_server(poisoned).run("streamable-http", host="0.0.0.0", port=int(os.getenv("MCP_PORT", "8000")))
