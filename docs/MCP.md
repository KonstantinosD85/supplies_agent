# MCP server

The MCP server (`src/supplies_agent/mcp_server/`) wraps the [Supplies API](SUPPLIES_API.md) as tools for the
agent. It is the agent's only way to reach the backend. It authenticates to the API, retries transient
failures, derives idempotency keys, and turns every failure into a message the model can act on.

- Transport: MCP streamable HTTP, **stateless**, JSON responses (no SSE).
- URL: `http://127.0.0.1:8002/mcp` locally; `http://mcp-server:8002/mcp` in compose (not published outside the
  compose network).
- Server name `lab-supplies`, with these instructions for the model: *"Tools for a lab's supplies: check stock
  on hand, search the catalogue of approved suppliers, and raise purchase orders. Check stock before proposing
  an order."*

Examples below are real responses (JSON-RPC over HTTP with curl), shortened where marked.

## Connecting

| Header | Required | Purpose |
|---|---|---|
| `X-User-Id` | for `create_purchase_order` | The caller. Becomes `requested_by` and part of the idempotency key |
| `X-Session-Id` | for `create_purchase_order` | The conversation. Part of the idempotency key |
| `Host` | yes | Must match `MCP_ALLOWED_HOSTS` (DNS-rebinding protection, on for every bind address) |

The agent service opens one MCP connection per request and sets the identity headers itself
(`agent/tools.py`), so the model never supplies a user or session. A request with a `Host` that isn't allowed
gets `421 Invalid Host header`.

```bash
curl -s http://127.0.0.1:8002/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'X-User-Id: alice' -H 'X-Session-Id: s1' \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
```

## Tools

| Tool | Annotations | Calls |
|---|---|---|
| [`check_stock`](#check_stock) | read-only, idempotent | `GET /stock` |
| [`search_catalog`](#search_catalog) | read-only, idempotent, paginated | `GET /catalog/items` |
| [`get_catalog_item`](#get_catalog_item) | read-only, idempotent | `GET /catalog/items/{sku}` |
| [`create_purchase_order`](#create_purchase_order) | not destructive, idempotent | `POST /purchase-orders` |

Every tool returns structured output (`structuredContent`, with an `outputSchema` in `tools/list`) plus the
same JSON as text. Several results carry a `hint` that tells the model what to do next.

### `check_stock`

> Check how many packs of an item the lab has on hand, and where they are stored.
> Use this first when the user asks for something, before searching the catalogue.

| Argument | Type | Description |
|---|---|---|
| `query` | string, 1–100, required | Item name or SKU, or part of one, e.g. 'PBS' or 'gloves'. |

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "check_stock", "arguments": {"query": "gloves"}}}
```

`structuredContent`:

```json
{
  "matches": [
    {"sku": "LC-GLOVE-M", "name": "Nitrile gloves, size M", "location": "Lab 3.12 shelf", "quantity": 0, "pack_size": "100 gloves"}
  ],
  "hint": null
}
```

No match:

```json
{"matches": [], "hint": "No stock records match 'unobtainium'. The lab may not hold this item; use search_catalog to find it."}
```

### `search_catalog`

> Search the catalogue of approved suppliers for items that can be ordered.
> Returns prices, pack sizes, lead times and the SKU needed to order. Results are paginated: if total_pages
> is greater than page, call again with the next page.

| Argument | Type | Default | Description |
|---|---|---|---|
| `query` | string ≤ 100, optional | | Words from the item name, or a SKU. Omit to list all. |
| `category` | `reagent` \| `consumable` \| `cell_culture`, optional | | Limit to one category. |
| `page` | int ≥ 1 | 1 | Page number, starting at 1. |
| `page_size` | int 1–20 | 10 | Items per page. |

```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
 "params": {"name": "search_catalog", "arguments": {"query": "tips", "page_size": 1}}}
```

```json
{
  "items": [
    {
      "sku": "LC-TIP-1000F",
      "name": "Filter pipette tips, 1000 uL",
      "category": "consumable",
      "pack_size": "10 x 96 racks",
      "unit_price": "84.00 EUR",
      "lead_time_days": 2,
      "supplier": "LabCore Direct"
    }
  ],
  "page": 1,
  "total_pages": 2,
  "total": 2,
  "hint": "More results: call again with page=2."
}
```

Compared with the API, prices are formatted (`"84.00 EUR"` instead of cents) and supplier ids are dropped:
the model gets what it needs to explain and order, nothing more.

| `hint` | When |
|---|---|
| `More results: call again with page=N.` | More pages follow |
| `Nothing matched. Try a shorter or more general query, or drop the category.` | No results |
| `Page N is past the end; there are M page(s).` | `page` beyond the last page |

### `get_catalog_item`

> Get the full details of one catalogue item by its SKU.

| Argument | Type | Description |
|---|---|---|
| `sku` | string, required | Catalogue SKU, e.g. 'BL-PBS-500'. |

Returns one catalogue entry, the same shape as an item of `search_catalog`.

### `create_purchase_order`

> Raise a purchase order with one approved supplier.
> All lines must come from the same supplier; split other items into separate orders. Use SKUs from
> search_catalog. The user is asked to approve the order before it is placed, so call this once you know
> exactly what to order.

| Argument | Type | Description |
|---|---|---|
| `lines` | 1–20 × `{sku, quantity}` | Items to order, one entry per SKU. `quantity`: number of packs, 1–1000. |
| `deliver_to` | string, 1–200, required | Delivery location, e.g. 'Lab 3.12'. Ask the user if you don't know it; never guess. |

The approval happens in the agent service: its hook stops the call until the user approves, so the tool only
runs for approved orders. The MCP server itself doesn't check approval.

```json
{"jsonrpc": "2.0", "id": 5, "method": "tools/call",
 "params": {"name": "create_purchase_order",
            "arguments": {"lines": [{"sku": "LC-TUBE-50", "quantity": 4}], "deliver_to": "Lab 3.12"}}}
```

```json
{
  "po_number": "PO-000002",
  "status": "submitted",
  "supplier": "LabCore Direct",
  "deliver_to": "Lab 3.12",
  "lines": [
    {"sku": "LC-TUBE-50", "name": "Conical centrifuge tubes, 50 mL", "quantity": 4, "unit_price": "15.00 EUR", "line_total": "60.00 EUR"}
  ],
  "total": "60.00 EUR",
  "created_at": "2026-10-18T14:15:12+00:00",
  "note": "Order placed."
}
```

**Idempotency key.** The server builds the order sent to the API (`requested_by` from `X-User-Id`, `deliver_to`,
lines sorted by SKU) and sends `Idempotency-Key = sha256([user_id, session_id, order])`. The model never sees or
sets the key. The same order in the same session therefore always has the same key. Calling it again returns
the existing PO with a different note:

```json
{
  "po_number": "PO-000002",
  "...": "...",
  "note": "This order was already recorded, by an earlier call in this session or an earlier attempt that timed out. No duplicate was created."
}
```

## Errors

A tool error comes back as a normal JSON-RPC result with `isError: true` and one text message. The message says
what went wrong **and what to do next**, so the model can act on it:

```json
{"jsonrpc": "2.0", "id": 4, "result": {
  "content": [{"type": "text", "text": "Error executing tool get_catalog_item: No catalogue item with SKU 'QC-ETOH-2L5'. Search the catalogue for valid SKUs."}],
  "isError": true}}
```

| Cause | Message to the model |
|---|---|
| API rejected the input (404, 409, 422) | The API's own `detail`, e.g. *"Unknown SKU(s): NOPE. Search the catalogue for valid SKUs."* or *"Lines span several suppliers (…). Create one purchase order per supplier."* |
| API unavailable after retries (read) | *"The supplies system is unavailable right now (4 attempts; last error: HTTP 503). Do not retry now. Tell the user you could not complete the request and suggest trying again in a few minutes."* |
| API unavailable after retries (write) | The same, plus *"The order may or may not have been recorded. Placing the same order again in this session is safe: it will not create a duplicate."* |
| Our API key rejected (401, 403) | *"The supplies system refused our credentials. Tell the user the supplies service is misconfigured and to contact support."* |
| No `X-User-Id` / `X-Session-Id` on an order | *"Ordering is unavailable: the request carries no user or session. Tell the user orders cannot be placed right now."* |
| Arguments outside the schema | The SDK's validation message, e.g. *"lines.0.quantity: Input should be greater than 0"* |

### Retries and timeouts

Calls to the API go through `SuppliesApi.request` (`mcp_server/client.py`):

- **Retried:** 429, 500, 502, 503, 504, connection errors, and attempts that exceed the per-attempt timeout.
  Other 4xx are not retried.
- **Backoff:** exponential from `backoff_base` (0.5 s, 1 s, 2 s…) plus jitter; a `Retry-After` header wins.
- **Limits:** `MCP_MAX_ATTEMPTS` (4), `MCP_ATTEMPT_TIMEOUT` (3 s per attempt), `MCP_TOTAL_TIMEOUT` (15 s for
  the whole call). Whichever runs out first ends the call with the "unavailable" error.
- `POST` is retried too: it's safe because every write carries the idempotency key.
- Each retry is logged (`supplies API call failed, retrying` with `attempt`, `error`, `retry_in_s`), and so is
  giving up.

Worked example with `server_error_rate: 1` on the mock. The 503 on a `POST` comes after the write commits, so
the order exists although the tool reported it as unavailable:

1. `create_purchase_order` (1 × `LC-GLOVE-M`) → 4 attempts, all 503 → `isError: true`, "…may or may not
   have been recorded…".
2. Faults off, the same call again → `PO-000003`, note *"This order was already recorded… No duplicate was
   created."* One order, not two.

## Telemetry

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set (compose sets it, with `OTEL_SERVICE_NAME=supplies-mcp`), the server
exports traces over OTLP (`mcp_server/telemetry.py`):

- The MCP SDK makes a `tools/call <tool>` server span per request. It joins the agent's trace through the trace
  context the agent sends in the request's `_meta`.
- Each attempt to call the supplies API is a child span, `supplies API <METHOD>`, with `supplies.attempt`,
  `url.path`, `http.response.status_code` and, for injected faults, `supplies.fault_injected`. Failed attempts
  have error status.
- `user.id` and `session.id` arrive as W3C baggage and are copied onto every span.

Logs stay on the console, each line with the current ` trace_id=<...>`.

## Configuration

| Variable | Default | |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8002` | Bind address |
| `MCP_ALLOWED_HOSTS` | `["127.0.0.1:*", "localhost:*", "[::1]:*"]` | JSON list of allowed `Host` headers |
| `MCP_API_URL` | `http://127.0.0.1:8001` | Supplies API |
| `MCP_API_KEY` | `dev-mock-key` | Sent as `X-API-Key` |
| `MCP_MAX_ATTEMPTS` | `4` | |
| `MCP_ATTEMPT_TIMEOUT` | `3.0` | seconds |
| `MCP_TOTAL_TIMEOUT` | `15.0` | seconds |
| `MCP_BACKOFF_BASE` | `0.5` | seconds |
