import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands import tool

from supplies_agent.agent.app import create_app
from supplies_agent.agent.dependencies import get_model, get_tool_source
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.mcp_server.server import build_server


@tool
def check_stock(query: str) -> dict:
    """Check stock."""
    return {"matches": [{"sku": "BL-PBS-500", "quantity": 6}]}


@pytest.fixture
def settings(tmp_path) -> AgentSettings:
    return AgentSettings(db_path=str(tmp_path / "agent.db"))


@pytest.fixture
def app_for(settings):
    """Build the agent app with a scripted model, wired through dependency overrides.

    tool_source stands in for the MCP tools; without it the agent connects to the MCP server
    at settings.mcp_url. Each call creates a new app on the same database, like a restart.
    """

    def make(model, tool_source=None, *, settings: AgentSettings = settings) -> FastAPI:
        app = create_app(settings)
        app.dependency_overrides[get_model] = lambda: model
        if tool_source is not None:
            app.dependency_overrides[get_tool_source] = lambda: tool_source
        return app

    return make


@pytest.fixture
def client_for(app_for):
    """Build a client whose agent uses the given model and local stand-in tools.

    Each call creates a new app on the same database, like a service restart.
    """

    def make(
        model,
        *,
        user_id="alice",
        session_id="session-1",
        seen: list | None = None,
        tools: list | None = None,
    ) -> TestClient:
        @contextmanager
        def tool_source(settings, ctx):
            if seen is not None:
                seen.append((ctx.user_id, ctx.session_id))
            yield tools if tools is not None else [check_stock]

        return TestClient(
            app_for(model, tool_source), headers={USER_HEADER: user_id, SESSION_HEADER: session_id}
        )

    return make


@pytest.fixture
def mcp_url(mcp_settings, api):
    """Serve the MCP server over real HTTP on a free port, backed by the in-process API."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = build_server(mcp_settings, api).streamable_http_app(
        stateless_http=True, json_response=True
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join()


@pytest.fixture
def mcp_agent_settings(settings, mcp_url) -> AgentSettings:
    """Agent settings pointing at the real MCP server (see mcp_url)."""
    return settings.model_copy(update={"mcp_url": mcp_url})
