"""The evals' fake supplies tools must behave like the real MCP tools, or the evals drift."""

import json
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from evals.fake_supplies import TOOL_SPECS, FakeSupplies
from supplies_agent.mcp_server.server import build_server

pytestmark = pytest.mark.anyio

ORDER = {"lines": [{"sku": "LC-GLOVE-M", "quantity": 2}], "deliver_to": "Lab 3.12"}
CALLS = [
    ("check_stock", {"query": "gloves"}),
    ("check_stock", {"query": "unobtainium"}),
    ("search_catalog", {"query": "tube"}),
    ("search_catalog", {"page_size": 5}),
    ("search_catalog", {"category": "reagent", "page": 2, "page_size": 3}),
    ("search_catalog", {"query": "ethanol"}),  # the unapproved supplier's item stays hidden
    ("get_catalog_item", {"sku": "BL-PBS-500"}),
    ("get_catalog_item", {"sku": "QC-ETOH-2L5"}),
    ("create_purchase_order", ORDER),
    ("create_purchase_order", {**ORDER, "lines": [{"sku": "NOPE", "quantity": 1}]}),
    (
        "create_purchase_order",
        {
            **ORDER,
            "lines": [{"sku": "LC-GLOVE-M", "quantity": 1}, {"sku": "BL-PBS-500", "quantity": 1}],
        },
    ),
]
VOLATILE = {"po_number", "created_at"}


def comparable(data):
    return {k: v for k, v in data.items() if k not in VOLATILE} if isinstance(data, dict) else data


@pytest.fixture
def server(mcp_settings, api):
    return build_server(mcp_settings, api)


@asynccontextmanager
async def mcp_client(server):
    """Over streamable HTTP with identity headers, as the agent service connects."""
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    async with server.session_manager.run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:8002",
            headers={"X-User-Id": "alice", "X-Session-Id": "s1"},
        ) as http:
            async with Client(
                streamable_http_client("http://127.0.0.1:8002/mcp", http_client=http)
            ) as client:
                yield client


async def test_fake_tools_have_the_real_definitions(server):
    async with Client(server) as client:
        real = {t.name: t for t in (await client.list_tools()).tools}
    assert set(real) == set(TOOL_SPECS)
    for name, spec in TOOL_SPECS.items():
        assert spec["description"] == real[name].description
        assert spec["inputSchema"]["json"] == real[name].input_schema


@pytest.mark.parametrize(
    ("name", "args"), CALLS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CALLS)]
)
async def test_fake_results_match_the_real_tools(server, name, args):
    async with mcp_client(server) as client:
        real = await client.call_tool(name, args)
    fake = FakeSupplies().call(name, args)

    assert (fake["status"] == "error") == bool(real.is_error)
    fake_text = fake["content"][0]["text"]
    if real.is_error:
        assert fake_text == real.content[0].text
    else:
        assert comparable(json.loads(fake_text)) == comparable(real.structured_content)
