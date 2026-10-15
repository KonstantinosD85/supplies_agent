"""/invocations end to end: scripted model -> agent -> MCP server (HTTP) -> supplies API."""

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from scripted_model import ScriptedModel, tool_call

from supplies_agent.agent.app import create_app
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.mcp_server.server import build_server

REQUEST = {"message": "Do we have PBS?"}
HEADERS = {USER_HEADER: "alice", SESSION_HEADER: "session-1"}


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


def chat(mcp_url, model, db_path) -> dict:
    app = create_app(AgentSettings(mcp_url=mcp_url, db_path=db_path), model_factory=lambda _: model)
    response = TestClient(app).post("/invocations", json=REQUEST, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_agent_reads_through_mcp(mcp_url, tmp_path):
    model = ScriptedModel(
        tool_call("check_stock", query="PBS"),
        tool_call("search_catalog", query="PBS"),
        "PBS is in stock.",
    )
    body = chat(mcp_url, model, str(tmp_path / "agent.db"))

    # The write tool stays hidden until the approval gate exists (D3).
    assert model.calls[0]["tools"] == ["check_stock", "get_catalog_item", "search_catalog"]
    assert [c["name"] for c in body["tool_calls"]] == ["check_stock", "search_catalog"]
    assert all(c["status"] == "success" for c in body["tool_calls"])
    assert '"quantity": 6' in body["tool_calls"][0]["result"]
    assert "18.50 EUR" in body["tool_calls"][1]["result"]


def test_tool_errors_reach_the_model(mcp_url, tmp_path):
    model = ScriptedModel(tool_call("get_catalog_item", sku="NOPE"), "That SKU does not exist.")
    body = chat(mcp_url, model, str(tmp_path / "agent.db"))

    [call] = body["tool_calls"]
    assert call["status"] == "error"
    assert "Search the catalogue for valid SKUs" in call["result"]
    assert "Search the catalogue for valid SKUs" in str(model.calls[1]["messages"][-1])
