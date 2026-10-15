"""MCP tools over the supplies API.

Tool descriptions, argument descriptions and error messages are written for the model:
each error says what went wrong and what to do next.
"""

import hashlib
import json
import logging
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from supplies_agent.logs import configure_logging
from supplies_agent.mcp_server.client import ApiRejected, ApiUnavailable, SuppliesApi
from supplies_agent.mcp_server.settings import McpSettings

logger = logging.getLogger(__name__)

USER_HEADER = "x-user-id"
SESSION_HEADER = "x-session-id"

Category = Literal["reagent", "consumable", "cell_culture"]

INSTRUCTIONS = """\
Tools for a lab's supplies: check stock on hand, search the catalogue of approved
suppliers, and raise purchase orders. Check stock before proposing an order.
"""

# --- Tool results ------------------------------------------------------------


class StockEntry(BaseModel):
    sku: str
    name: str
    location: str
    quantity: int = Field(description="Packs on hand.")
    pack_size: str


class StockResult(BaseModel):
    matches: list[StockEntry]
    hint: str | None = None


class CatalogEntry(BaseModel):
    sku: str
    name: str
    category: Category
    pack_size: str
    unit_price: str = Field(description="Price per pack, e.g. '18.50 EUR'.")
    lead_time_days: int
    supplier: str


class CatalogPage(BaseModel):
    items: list[CatalogEntry]
    page: int
    total_pages: int
    total: int
    hint: str | None = None


class OrderLine(BaseModel):
    sku: str = Field(description="Catalogue SKU, e.g. 'BL-PBS-500'.")
    quantity: int = Field(gt=0, le=1000, description="Number of packs.")


class OrderedLine(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_price: str
    line_total: str


class PurchaseOrderResult(BaseModel):
    po_number: str
    status: str
    supplier: str
    deliver_to: str | None
    lines: list[OrderedLine]
    total: str
    created_at: str
    note: str


# --- Helpers -------------------------------------------------------------------


def _money(cents: int, currency: str) -> str:
    return f"{cents // 100}.{cents % 100:02d} {currency}"


def _catalog_entry(item: dict[str, Any]) -> CatalogEntry:
    return CatalogEntry(
        sku=item["sku"],
        name=item["name"],
        category=item["category"],
        pack_size=item["pack_size"],
        unit_price=_money(item["unit_price_cents"], item["currency"]),
        lead_time_days=item["lead_time_days"],
        supplier=item["supplier_name"],
    )


def _unavailable(e: ApiUnavailable, *, write: bool = False) -> ToolError:
    message = (
        f"The supplies system is unavailable right now ({e.attempts} attempts; last error: "
        f"{e.last_error}). Do not retry now. Tell the user you could not complete the request "
        "and suggest trying again in a few minutes."
    )
    if write:
        message += (
            " The order may or may not have been recorded. Placing the same order again in this "
            "session is safe: it will not create a duplicate."
        )
    return ToolError(message)


def _rejected(e: ApiRejected) -> ToolError:
    if e.status in (401, 403):
        # Our credentials, not the model's input: nothing the model can fix.
        return ToolError(
            "The supplies system refused our credentials. Tell the user the supplies service is "
            "misconfigured and to contact support."
        )
    return ToolError(e.detail)


def _identity(ctx: Context) -> tuple[str, str]:
    """The user and session set by the agent service on the MCP connection, never by the model."""
    headers = ctx.headers or {}
    user_id, session_id = headers.get(USER_HEADER), headers.get(SESSION_HEADER)
    if not user_id or not session_id:
        raise ToolError(
            "Ordering is unavailable: the request carries no user or session. Tell the user "
            "orders cannot be placed right now."
        )
    return user_id, session_id


def idempotency_key(user_id: str, session_id: str, order: dict[str, Any]) -> str:
    """Same user, session and order -> same key, so the API never creates the order twice."""
    payload = json.dumps([user_id, session_id, order], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# --- Server ----------------------------------------------------------------------


def build_server(settings: McpSettings, api: SuppliesApi | None = None) -> MCPServer:
    api = api or SuppliesApi(settings)
    mcp = MCPServer("lab-supplies", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    @mcp.tool(annotations=read_only)
    async def check_stock(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                description="Item name or SKU, or part of one, e.g. 'PBS' or 'gloves'.",
            ),
        ],
    ) -> StockResult:
        """Check how many packs of an item the lab has on hand, and where they are stored.

        Use this first when the user asks for something, before searching the catalogue.
        """
        try:
            response = await api.request("GET", "/stock", params={"q": query})
        except ApiUnavailable as e:
            raise _unavailable(e) from e
        except ApiRejected as e:
            raise _rejected(e) from e
        matches = [StockEntry(**row) for row in response.json()]
        hint = None
        if not matches:
            hint = (
                f"No stock records match {query!r}. The lab may not hold this item; "
                "use search_catalog to find it."
            )
        return StockResult(matches=matches, hint=hint)

    @mcp.tool(annotations=read_only)
    async def search_catalog(
        query: Annotated[
            str | None,
            Field(
                max_length=100, description="Words from the item name, or a SKU. Omit to list all."
            ),
        ] = None,
        category: Annotated[Category | None, Field(description="Limit to one category.")] = None,
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
        page_size: Annotated[int, Field(ge=1, le=20, description="Items per page.")] = 10,
    ) -> CatalogPage:
        """Search the catalogue of approved suppliers for items that can be ordered.

        Returns prices, pack sizes, lead times and the SKU needed to order. Results are
        paginated: if total_pages is greater than page, call again with the next page.
        """
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if query:
            params["q"] = query
        if category:
            params["category"] = category
        try:
            response = await api.request("GET", "/catalog/items", params=params)
        except ApiUnavailable as e:
            raise _unavailable(e) from e
        except ApiRejected as e:
            raise _rejected(e) from e

        body = response.json()
        hint = None
        if body["total"] == 0:
            hint = "Nothing matched. Try a shorter or more general query, or drop the category."
        elif page < body["total_pages"]:
            hint = f"More results: call again with page={page + 1}."
        elif page > body["total_pages"]:
            hint = f"Page {page} is past the end; there are {body['total_pages']} page(s)."
        return CatalogPage(
            items=[_catalog_entry(item) for item in body["items"]],
            page=body["page"],
            total_pages=body["total_pages"],
            total=body["total"],
            hint=hint,
        )

    @mcp.tool(annotations=read_only)
    async def get_catalog_item(
        sku: Annotated[str, Field(min_length=1, description="Catalogue SKU, e.g. 'BL-PBS-500'.")],
    ) -> CatalogEntry:
        """Get the full details of one catalogue item by its SKU."""
        try:
            response = await api.request("GET", f"/catalog/items/{sku}")
        except ApiUnavailable as e:
            raise _unavailable(e) from e
        except ApiRejected as e:
            raise _rejected(e) from e
        return _catalog_entry(response.json())

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    async def create_purchase_order(
        lines: Annotated[
            list[OrderLine],
            Field(min_length=1, max_length=20, description="Items to order, one entry per SKU."),
        ],
        ctx: Context,
        deliver_to: Annotated[
            str | None,
            Field(max_length=200, description="Delivery location, e.g. 'Lab 3.12'."),
        ] = None,
    ) -> PurchaseOrderResult:
        """Raise a purchase order with one approved supplier.

        All lines must come from the same supplier; split other items into separate orders.
        Use SKUs from search_catalog. The user is asked to approve the order before it is
        placed, so call this once you know exactly what to order.
        """
        user_id, session_id = _identity(ctx)
        order = {
            "requested_by": user_id,
            "deliver_to": deliver_to,
            # Sorted, so the same order always has the same body and the same key.
            "lines": sorted((line.model_dump() for line in lines), key=lambda line: line["sku"]),
        }
        key = idempotency_key(user_id, session_id, order)
        try:
            response = await api.request(
                "POST", "/purchase-orders", json=order, headers={"Idempotency-Key": key}
            )
        except ApiUnavailable as e:
            raise _unavailable(e, write=True) from e
        except ApiRejected as e:
            raise _rejected(e) from e

        po = response.json()
        replayed = response.headers.get("Idempotent-Replayed") == "true"
        logger.info(
            "po_number=<%s>, user_id=<%s>, session_id=<%s> | %s",
            po["po_number"],
            user_id,
            session_id,
            "duplicate order blocked, existing purchase order returned"
            if replayed
            else "purchase order created",
        )
        currency = po["currency"]
        return PurchaseOrderResult(
            po_number=po["po_number"],
            status=po["status"],
            supplier=po["supplier_name"],
            deliver_to=po["deliver_to"],
            lines=[
                OrderedLine(
                    sku=line["sku"],
                    name=line["name"],
                    quantity=line["quantity"],
                    unit_price=_money(line["unit_price_cents"], currency),
                    line_total=_money(line["line_total_cents"], currency),
                )
                for line in po["lines"]
            ],
            total=_money(po["total_cents"], currency),
            created_at=po["created_at"],
            note=(
                "This order was already recorded, by an earlier call in this session or an earlier "
                "attempt that timed out. No duplicate was created."
                if replayed
                else "Order placed."
            ),
        )

    return mcp


def main() -> None:
    configure_logging()
    settings = McpSettings()
    build_server(settings).run(
        "streamable-http",
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        json_response=True,
    )
