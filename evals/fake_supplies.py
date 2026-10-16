"""Fake supplies tools for evals: the real MCP tool definitions, with results from seed data.

The model sees exactly the tools it sees in production (names, descriptions, input schemas are
read from the MCP server), and results have the MCP server's shapes and error messages. Only
the backend is fake, so each case controls what the tools return and nothing touches a network.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from strands.tools.tools import PythonAgentTool
from strands.types.tools import ToolResult, ToolUse

from supplies_agent.api import seed
from supplies_agent.mcp_server.client import ApiUnavailable
from supplies_agent.mcp_server.server import (
    CatalogEntry,
    CatalogPage,
    OrderedLine,
    PurchaseOrderResult,
    StockEntry,
    StockResult,
    _money,
    _unavailable,
    build_server,
)
from supplies_agent.mcp_server.settings import McpSettings

CURRENCY = "EUR"
ALL_TOOLS = "*"


def _mcp_tool_specs() -> dict[str, dict]:
    """The MCP server's tool definitions, as Strands tool specs. Read in-process, no network."""
    tools = asyncio.run(build_server(McpSettings(api_key="unused")).list_tools())
    return {
        t.name: {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": {"json": t.input_schema},
        }
        for t in tools
    }


TOOL_SPECS = _mcp_tool_specs()


def _default_catalogue() -> dict[str, dict]:
    approved = {sid: name for sid, name, ok in seed.SUPPLIERS if ok}
    return {
        sku: {
            "sku": sku,
            "name": name,
            "category": category,
            "supplier_id": supplier_id,
            "supplier": approved[supplier_id],
            "pack_size": pack_size,
            "unit_price_cents": price,
            "lead_time_days": lead_time,
        }
        for sku, name, category, supplier_id, pack_size, price, lead_time in seed.CATALOG_ITEMS
        if supplier_id in approved  # like the mock: unapproved suppliers are not orderable
    }


def _default_stock() -> dict[str, tuple[str, int]]:
    return {sku: (location, quantity) for sku, location, quantity in seed.STOCK}


@dataclass
class FakeSupplies:
    """One case's supplies backend.

    Args:
        stock: Overrides on the seed stock, sku -> quantity (location kept from the seed).
        unavailable: Tool names that fail as if the supplies system were down ("*" for all).
    """

    stock: dict[str, int] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._catalogue = _default_catalogue()
        self._stock = _default_stock()
        for sku, quantity in self.stock.items():
            location = self._stock.get(sku, ("Store room", 0))[0]
            self._stock[sku] = (location, quantity)

    def tools(self) -> list[PythonAgentTool]:
        handlers = {
            "check_stock": self._check_stock,
            "search_catalog": self._search_catalog,
            "get_catalog_item": self._get_catalog_item,
            "create_purchase_order": self._create_purchase_order,
        }
        assert set(handlers) == set(TOOL_SPECS), "fake tools out of sync with the MCP server"
        return [
            PythonAgentTool(name, TOOL_SPECS[name], self._wrap(name, handler))
            for name, handler in handlers.items()
        ]

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Call one tool directly, as the agent would (used by the contract test)."""
        tool = next(t for t in self.tools() if t.tool_name == name)
        return tool._tool_func({"toolUseId": "direct", "name": name, "input": args})

    def _wrap(self, name: str, handler):
        def run(tool_use: ToolUse, **_: Any) -> ToolResult:
            args = tool_use["input"]
            self.calls.append((name, args))
            if name in self.unavailable or ALL_TOOLS in self.unavailable:
                error = _unavailable(
                    ApiUnavailable(attempts=4, last_error="503 Service Unavailable"),
                    write=name == "create_purchase_order",
                )
                return _error(tool_use, name, str(error))
            try:
                body = handler(**args)
            except ValueError as e:  # the API's 4xx detail, passed to the model as is
                return _error(tool_use, name, str(e))
            return _result(tool_use, "success", body.model_dump_json())

        return run

    # --- Tool behaviour, mirroring the mock API and the MCP server ---------------------------

    def _matches(self, query: str | None, item: dict) -> bool:
        q = (query or "").lower()
        return q in item["name"].lower() or q in item["sku"].lower()

    def _check_stock(self, query: str) -> StockResult:
        matches = [
            StockEntry(
                sku=sku,
                name=self._catalogue[sku]["name"],
                location=location,
                quantity=quantity,
                pack_size=self._catalogue[sku]["pack_size"],
            )
            for sku, (location, quantity) in sorted(self._stock.items())
            if sku in self._catalogue and self._matches(query, self._catalogue[sku])
        ]
        hint = None
        if not matches:
            hint = (
                f"No stock records match {query!r}. The lab may not hold this item; "
                "use search_catalog to find it."
            )
        return StockResult(matches=matches, hint=hint)

    def _entry(self, item: dict) -> CatalogEntry:
        return CatalogEntry(
            sku=item["sku"],
            name=item["name"],
            category=item["category"],
            pack_size=item["pack_size"],
            unit_price=_money(item["unit_price_cents"], CURRENCY),
            lead_time_days=item["lead_time_days"],
            supplier=item["supplier"],
        )

    def _search_catalog(
        self,
        query: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> CatalogPage:
        found = [
            item
            for _, item in sorted(self._catalogue.items())
            if self._matches(query, item) and (category is None or item["category"] == category)
        ]
        total_pages = max(1, -(-len(found) // page_size))
        items = found[(page - 1) * page_size : page * page_size]
        hint = None
        if not found:
            hint = "Nothing matched. Try a shorter or more general query, or drop the category."
        elif page < total_pages:
            hint = f"More results: call again with page={page + 1}."
        return CatalogPage(
            items=[self._entry(i) for i in items],
            page=page,
            total_pages=total_pages,
            total=len(found),
            hint=hint,
        )

    def _get_catalog_item(self, sku: str) -> CatalogEntry:
        if sku not in self._catalogue:
            raise ValueError(
                f"No catalogue item with SKU {sku!r}. Search the catalogue for valid SKUs."
            )
        return self._entry(self._catalogue[sku])

    def _create_purchase_order(self, lines: list[dict], deliver_to: str) -> PurchaseOrderResult:
        skus = [line["sku"] for line in lines]
        unknown = [sku for sku in skus if sku not in self._catalogue]
        if unknown:
            raise ValueError(
                f"Unknown SKU(s): {', '.join(unknown)}. Search the catalogue for valid SKUs."
            )
        suppliers = sorted({self._catalogue[sku]["supplier_id"] for sku in skus})
        if len(suppliers) > 1:
            raise ValueError(
                f"Lines span several suppliers ({', '.join(suppliers)}). "
                "Create one purchase order per supplier."
            )
        self.orders.append({"lines": lines, "deliver_to": deliver_to})
        ordered = []
        for line in lines:
            item = self._catalogue[line["sku"]]
            ordered.append(
                OrderedLine(
                    sku=item["sku"],
                    name=item["name"],
                    quantity=line["quantity"],
                    unit_price=_money(item["unit_price_cents"], CURRENCY),
                    line_total=_money(item["unit_price_cents"] * line["quantity"], CURRENCY),
                )
            )
        total = sum(
            self._catalogue[line["sku"]]["unit_price_cents"] * line["quantity"] for line in lines
        )
        return PurchaseOrderResult(
            po_number=f"PO-{len(self.orders):06d}",
            status="submitted",
            supplier=self._catalogue[skus[0]]["supplier"],
            deliver_to=deliver_to,
            lines=ordered,
            total=_money(total, CURRENCY),
            created_at="2026-01-01T09:00:00Z",
            note="Order placed.",
        )


def _result(tool_use: ToolUse, status: str, text: str) -> ToolResult:
    return {"toolUseId": tool_use["toolUseId"], "status": status, "content": [{"text": text}]}


def _error(tool_use: ToolUse, name: str, message: str) -> ToolResult:
    # Worded as the MCP server words a tool error, so the model sees the same text.
    return _result(tool_use, "error", f"Error executing tool {name}: {message}")
