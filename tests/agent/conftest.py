import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi.testclient import TestClient
from strands import tool

from supplies_agent.agent.app import create_app
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
def client_for(settings):
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

        app = create_app(settings, model_factory=lambda _: model, tool_source=tool_source)
        return TestClient(app, headers={USER_HEADER: user_id, SESSION_HEADER: session_id})

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
