"""/invocations end to end: scripted model -> agent -> MCP server (HTTP) -> supplies API."""

import pytest
from fastapi.testclient import TestClient
from scripted_model import ScriptedModel, tool_call

from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER

REQUEST = {"message": "Do we have PBS?"}
HEADERS = {USER_HEADER: "alice", SESSION_HEADER: "session-1"}


@pytest.fixture
def chat(app_for, mcp_agent_settings):
    """Send one request on a fresh app (same database), like a restart between requests."""

    def send(model, body: dict = REQUEST) -> dict:
        app = app_for(model, settings=mcp_agent_settings)
        response = TestClient(app).post("/invocations", json=body, headers=HEADERS)
        assert response.status_code == 200, response.text
        return response.json()

    return send


@pytest.fixture
def decide(chat):
    """Answer the one pending order of a proposal."""

    def send(model, proposed: dict, decision: str) -> dict:
        [pending] = proposed["pending_approvals"]
        approvals = [{"interrupt_id": pending["interrupt_id"], "decision": decision}]
        return chat(model, {"approvals": approvals})

    return send


async def purchase_orders(api) -> int:
    return (await api.request("GET", "/purchase-orders")).json()["total"]


def test_agent_reads_through_mcp(chat):
    model = ScriptedModel(
        tool_call("check_stock", query="PBS"),
        tool_call("search_catalog", query="PBS"),
        "PBS is in stock.",
    )
    body = chat(model)

    assert model.calls[0]["tools"] == [
        "check_stock",
        "create_purchase_order",
        "get_catalog_item",
        "remember_fact",
        "search_catalog",
    ]
    assert [c["name"] for c in body["tool_calls"]] == ["check_stock", "search_catalog"]
    assert all(c["status"] == "success" for c in body["tool_calls"])
    assert '"quantity": 6' in body["tool_calls"][0]["result"]
    assert "18.50 EUR" in body["tool_calls"][1]["result"]


def test_tool_errors_reach_the_model(chat):
    model = ScriptedModel(tool_call("get_catalog_item", sku="NOPE"), "That SKU does not exist.")
    body = chat(model)

    [call] = body["tool_calls"]
    assert call["status"] == "error"
    assert "Search the catalogue for valid SKUs" in call["result"]
    assert "Search the catalogue for valid SKUs" in str(model.calls[1]["messages"][-1])


ORDER = tool_call(
    "create_purchase_order", lines=[{"sku": "LC-GLOVE-M", "quantity": 2}], deliver_to="Lab 3.12"
)


@pytest.mark.anyio
async def test_no_purchase_order_exists_before_approval(chat, api):
    proposed = chat(ScriptedModel(ORDER))

    assert proposed["stop_reason"] == "interrupt"
    assert await purchase_orders(api) == 0


@pytest.mark.anyio
async def test_approved_order_is_placed_exactly_once_with_faults_on(chat, decide, api):
    model = ScriptedModel(ORDER, "The supplies system is down; please try again later.")
    proposed = chat(model)

    # Every write commits and then fails with a 503, so the MCP server retries until it gives up.
    await api.request("PUT", "/admin/faults", json={"server_error_rate": 1})
    approved = decide(model, proposed, "approve")

    [call] = approved["tool_calls"]
    assert call["status"] == "error"
    assert "will not create a duplicate" in call["result"]
    await api.request("PUT", "/admin/faults", json={})
    assert await purchase_orders(api) == 1  # retried several times, recorded once


@pytest.mark.anyio
async def test_approved_order_is_placed(chat, decide, api):
    model = ScriptedModel(ORDER, "Placed PO-000001.")
    approved = decide(model, chat(model), "approve")

    [call] = approved["tool_calls"]
    assert call["status"] == "success"
    assert "PO-000001" in call["result"]
    assert await purchase_orders(api) == 1


@pytest.mark.anyio
async def test_declined_order_is_never_placed(chat, decide, api):
    model = ScriptedModel(ORDER, "Not placed.")
    decide(model, chat(model), "decline")
    assert await purchase_orders(api) == 0
