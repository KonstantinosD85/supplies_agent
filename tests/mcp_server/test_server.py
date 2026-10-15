"""MCP tools end to end: MCP client -> MCP server -> real supplies API (in-process)."""

from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from supplies_agent.mcp_server.server import build_server, idempotency_key

pytestmark = pytest.mark.anyio

IDENTITY = {"X-User-Id": "alice", "X-Session-Id": "session-1"}


@pytest.fixture
def server(mcp_settings, api):
    return build_server(mcp_settings, api)


@asynccontextmanager
async def http_client(server, headers):
    """Connect over streamable HTTP, as the agent service will, with identity headers."""
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    async with server.session_manager.run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:8002",
            headers=headers,
        ) as http:
            async with Client(
                streamable_http_client("http://127.0.0.1:8002/mcp", http_client=http)
            ) as c:
                yield c


def text(result) -> str:
    return result.content[0].text


async def test_lists_the_four_tools(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "check_stock",
        "search_catalog",
        "get_catalog_item",
        "create_purchase_order",
    }
    assert tools["check_stock"].annotations.read_only_hint is True
    # Identity and idempotency key are never arguments the model can set.
    order_args = tools["create_purchase_order"].input_schema["properties"]
    assert set(order_args) == {"lines", "deliver_to"}


async def test_check_stock(server):
    async with Client(server) as client:
        found = await client.call_tool("check_stock", {"query": "gloves"})
        missing = await client.call_tool("check_stock", {"query": "unobtainium"})
    assert found.structured_content["matches"][0]["sku"] == "LC-GLOVE-M"
    assert missing.structured_content["matches"] == []
    assert "search_catalog" in missing.structured_content["hint"]


async def test_search_catalog_paginates_with_hints(server):
    async with Client(server) as client:
        first = await client.call_tool("search_catalog", {"page_size": 5})
        last = await client.call_tool("search_catalog", {"page": 3, "page_size": 5})
    assert first.structured_content["total_pages"] == 3
    assert first.structured_content["hint"] == "More results: call again with page=2."
    assert first.structured_content["items"][0]["unit_price"].endswith(" EUR")
    assert last.structured_content["hint"] is None


async def test_page_size_above_limit_is_rejected_by_schema(server):
    async with Client(server) as client:
        result = await client.call_tool("search_catalog", {"page_size": 50})
    assert result.is_error


async def test_unknown_sku_returns_actionable_error(server):
    async with Client(server) as client:
        result = await client.call_tool("get_catalog_item", {"sku": "QC-ETOH-2L5"})
    assert result.is_error
    assert "Search the catalogue" in text(result)


async def test_api_outage_tells_model_to_stop(server, api):
    await api.request("PUT", "/admin/faults", json={"rate_limit_rate": 1})
    async with Client(server) as client:
        result = await client.call_tool("check_stock", {"query": "PBS"})
    assert result.is_error
    assert "Do not retry now" in text(result)


async def test_order_needs_user_and_session(server):
    async with Client(server) as client:  # in-process: no HTTP headers
        result = await client.call_tool(
            "create_purchase_order", {"lines": [{"sku": "LC-GLOVE-M", "quantity": 1}]}
        )
    assert result.is_error
    assert "no user or session" in text(result)


async def test_create_purchase_order_over_http(server, api):
    order = {"lines": [{"sku": "LC-GLOVE-M", "quantity": 2}], "deliver_to": "Lab 3.12"}
    async with http_client(server, IDENTITY) as client:
        first = await client.call_tool("create_purchase_order", order)
        again = await client.call_tool("create_purchase_order", order)

    po = first.structured_content
    assert not first.is_error, text(first)
    assert (po["po_number"], po["total"], po["note"]) == ("PO-000001", "24.00 EUR", "Order placed.")
    assert again.structured_content["po_number"] == "PO-000001"
    assert "No duplicate" in again.structured_content["note"]

    stored = (await api.request("GET", "/purchase-orders")).json()
    assert stored["total"] == 1
    assert stored["items"][0]["requested_by"] == "alice"


async def test_same_order_in_another_session_is_a_new_order(server, api):
    order = {"lines": [{"sku": "LC-GLOVE-M", "quantity": 2}]}
    async with http_client(server, IDENTITY) as client:
        await client.call_tool("create_purchase_order", order)
    async with http_client(server, {**IDENTITY, "X-Session-Id": "session-2"}) as client:
        await client.call_tool("create_purchase_order", order)
    assert (await api.request("GET", "/purchase-orders")).json()["total"] == 2


async def test_order_rejected_by_api_reaches_the_model(server):
    lines = [{"sku": "LC-GLOVE-M", "quantity": 1}, {"sku": "BL-PBS-500", "quantity": 1}]
    async with http_client(server, IDENTITY) as client:
        result = await client.call_tool("create_purchase_order", {"lines": lines})
    assert result.is_error
    assert "one purchase order per supplier" in text(result)


async def test_order_outage_says_retrying_is_safe(server, api):
    await api.request("PUT", "/admin/faults", json={"server_error_rate": 1})
    async with http_client(server, IDENTITY) as client:
        result = await client.call_tool(
            "create_purchase_order", {"lines": [{"sku": "LC-GLOVE-M", "quantity": 1}]}
        )
    assert result.is_error
    assert "will not create a duplicate" in text(result)


def test_idempotency_key_depends_on_user_session_and_order():
    order = {"requested_by": "alice", "deliver_to": None, "lines": [{"sku": "A", "quantity": 1}]}
    bigger = {**order, "lines": [{"sku": "A", "quantity": 2}]}
    key = idempotency_key("alice", "s1", order)
    assert key == idempotency_key("alice", "s1", dict(reversed(order.items())))  # dict order
    assert key != idempotency_key("alice", "s2", order)
    assert key != idempotency_key("bob", "s1", order)
    assert key != idempotency_key("alice", "s1", bigger)
