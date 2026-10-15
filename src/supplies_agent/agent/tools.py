"""Supplies tools from the MCP server, connected per request with the caller's identity."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from strands.tools.mcp import MCPClient

from supplies_agent.agent.schemas import RequestContext
from supplies_agent.agent.settings import AgentSettings

# Not exposed until the approval gate exists (docs/PLAN.md, D3).
DISABLED_TOOLS = ["create_purchase_order"]


@contextmanager
def mcp_tools(settings: AgentSettings, ctx: RequestContext) -> Generator[list[Any], None, None]:
    """Open an MCP connection for one request and yield its tools.

    One connection per request keeps identity per user: the MCP server reads
    X-User-Id / X-Session-Id from the connection, never from the model.
    """
    client = MCPClient(
        url=settings.mcp_url,
        headers={"X-User-Id": ctx.user_id, "X-Session-Id": ctx.session_id},
        tool_filters={"rejected": DISABLED_TOOLS},
    )
    with client:
        yield client.list_tools_sync()
