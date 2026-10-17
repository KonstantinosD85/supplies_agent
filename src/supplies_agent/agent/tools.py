"""Supplies tools from the MCP server, connected per request with the caller's identity."""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from strands.tools.mcp import MCPClient
from strands.types.exceptions import MCPClientInitializationError

from supplies_agent.agent.schemas import RequestContext
from supplies_agent.agent.settings import AgentSettings

logger = logging.getLogger(__name__)


@contextmanager
def mcp_tools(settings: AgentSettings, ctx: RequestContext) -> Generator[list[Any]]:
    """Open an MCP connection for one request and yield its tools.

    One connection per request keeps identity per user: the MCP server reads
    X-User-Id / X-Session-Id from the connection, never from the model.
    """
    client = MCPClient(
        url=settings.mcp_url,
        headers={"X-User-Id": ctx.user_id, "X-Session-Id": ctx.session_id},
    )
    try:
        client.start()
    except MCPClientInitializationError:
        logger.exception(
            "MCP server unreachable", extra={**ctx.log_fields(), "mcp_url": settings.mcp_url}
        )
        raise
    try:
        yield client.list_tools_sync()
    finally:
        client.stop(None, None, None)
