# Supplies API (mock)

The mock inventory and ordering service (`src/supplies_agent/api/`): FastAPI on SQLite, seeded with a few
suppliers, catalogue items and stock rows (`api/seed.py`). Only the [MCP server](MCP.md) calls it; the
agent never does.

- Base URL: `http://127.0.0.1:8001` (local and compose; in compose the MCP server uses `http://mock-api:8001`)
- Format: JSON in and out. Prices are integer cents plus a `currency` (`EUR`).
- Interactive docs: `GET /docs` (OpenAPI, from FastAPI).

Examples below are real responses from a fresh database.

## Authentication

Every endpoint except `/health` and the docs needs the shared key in `X-API-Key` (`API_KEY`, default
`dev-mock-key`; compose sets it from `MOCK_API_KEY`). The key is compared in constant time.

```bash
curl -s http://127.0.0.1:8001/stock
```

```json
401 {"detail": "Missing or invalid X-API-Key header."}
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, no auth |
| `GET` | `/stock` | Stock on hand |
| `GET` | `/catalog/items` | Search the catalogue (approved suppliers only), paginated |
| `GET` | `/catalog/items/{sku}` | One catalogue item |
| `POST` | `/purchase-orders` | Create a purchase order (idempotent) |
| `GET` | `/purchase-orders` | List purchase orders, paginated |
| `GET` | `/purchase-orders/{po_number}` | One purchase order |
| `GET` / `PUT` | `/admin/faults` | Read / set fault injection |

### `GET /health`

```json
200 {"status": "ok"}
```

### `GET /stock`

| Query | Type | Notes |
|---|---|---|
| `q` | string, ≤ 100 | Case-insensitive substring of the item name or SKU. Omit for everything. |

```bash
curl -s 'http://127.0.0.1:8001/stock?q=PBS' -H 'X-API-Key: dev-mock-key'
```

```json
[
  {"sku": "BL-PBS-500", "name": "PBS 1X, pH 7.4, sterile", "location": "Cold room 2", "quantity": 6, "pack_size": "500 mL"}
]
```

`quantity` is packs on hand. No match returns `[]`.

### `GET /catalog/items`

Only items from **approved** suppliers are returned (QuickChem Discount is seeded as not approved).

| Query | Type | Default | Notes |
|---|---|---|---|
| `q` | string, ≤ 100 | | Substring of the name or SKU |
| `category` | `reagent` \| `consumable` \| `cell_culture` | | |
| `page` | int ≥ 1 | 1 | |
| `page_size` | int 1–20 | 10 | |

```bash
curl -s 'http://127.0.0.1:8001/catalog/items?q=tips&page_size=1' -H 'X-API-Key: dev-mock-key'
```

```json
{
  "items": [
    {
      "sku": "LC-TIP-1000F",
      "name": "Filter pipette tips, 1000 uL",
      "category": "consumable",
      "pack_size": "10 x 96 racks",
      "unit_price_cents": 8400,
      "currency": "EUR",
      "lead_time_days": 2,
      "supplier_id": "SUP-LC",
      "supplier_name": "LabCore Direct"
    }
  ],
  "page": 1,
  "page_size": 1,
  "total": 2,
  "total_pages": 2
}
```

Results are ordered by SKU. A page past the end returns an empty `items` list.

### `GET /catalog/items/{sku}`

Returns one item in the same shape as above. An unknown SKU, or one from an unapproved supplier:

```json
404 {"detail": "No catalogue item with SKU 'QC-ETOH-2L5'. Search the catalogue for valid SKUs."}
```

### `POST /purchase-orders`

| Header | Required | Notes |
|---|---|---|
| `Idempotency-Key` | yes, 8–200 chars | Same key + same body returns the existing PO instead of creating one |

Body (unknown fields are rejected):

| Field | Type | Notes |
|---|---|---|
| `requested_by` | string, required | User id of the requester |
| `deliver_to` | string, 1–200, required | Delivery location |
| `lines` | 1–20 × `{sku, quantity}` | `quantity` 1–1000 packs; each SKU at most once |

All lines must come from one approved supplier.

```bash
curl -s -X POST http://127.0.0.1:8001/purchase-orders \
  -H 'X-API-Key: dev-mock-key' -H 'Idempotency-Key: demo-key-0001' -H 'Content-Type: application/json' \
  -d '{"requested_by": "alice", "deliver_to": "Lab 3.12", "lines": [{"sku": "LC-GLOVE-M", "quantity": 2}]}'
```

`201 Created`:

```json
{
  "po_number": "PO-000001",
  "status": "submitted",
  "supplier_id": "SUP-LC",
  "supplier_name": "LabCore Direct",
  "requested_by": "alice",
  "deliver_to": "Lab 3.12",
  "lines": [
    {"sku": "LC-GLOVE-M", "name": "Nitrile gloves, size M", "quantity": 2, "unit_price_cents": 1200, "line_total_cents": 2400}
  ],
  "total_cents": 2400,
  "currency": "EUR",
  "created_at": "2026-10-18T14:14:52+00:00"
}
```

**Idempotency.** The key check and the insert run in one `BEGIN IMMEDIATE` transaction, so concurrent
retries can't both insert.

- Same key, same body: `200 OK` with the existing PO and the header `Idempotent-Replayed: true`.
- Same key, different body:

  ```json
  409 {"detail": "This Idempotency-Key was already used for a different order. Use a new key for a new order."}
  ```

Errors:

| Status | When | Body |
|---|---|---|
| 422 | Unknown SKU, or one from an unapproved supplier | `{"detail": "Unknown SKU(s): NOPE. Search the catalogue for valid SKUs."}` |
| 422 | Lines from more than one supplier | `{"detail": "Lines span several suppliers (SUP-BL, SUP-LC). Create one purchase order per supplier."}` |
| 422 | Same SKU twice | `{"detail": "Each SKU may appear only once per order. Combine the quantities."}` |
| 422 | Missing header or invalid body (FastAPI validation) | `{"detail": [{"type": "missing", "loc": ["header", "idempotency-key"], "msg": "Field required", ...}]}` |
| 409 | Key reused for a different order | see above |

### `GET /purchase-orders`

| Query | Type | Default |
|---|---|---|
| `requested_by` | string | all users |
| `page` | int ≥ 1 | 1 |
| `page_size` | int 1–20 | 10 |

Returns a page (`items`, `page`, `page_size`, `total`, `total_pages`) of purchase orders in the shape above.

### `GET /purchase-orders/{po_number}`

One purchase order. Unknown or malformed numbers:

```json
404 {"detail": "No purchase order 'PO-999999'. PO numbers look like PO-000001."}
```

## Fault injection

`GET /admin/faults` returns the current settings; `PUT /admin/faults` replaces them (fields left out reset to
their defaults). Each fault is rolled independently on every request, except `/health`, `/admin/*` and the docs.

| Field | Range | Default | Effect |
|---|---|---|---|
| `rate_limit_rate` | 0–1 | 0 | Chance of `429` with `Retry-After: 1` |
| `server_error_rate` | 0–1 | 0 | Chance of `503`. On a `POST` the 503 is returned **after the write commits** |
| `slow_rate` | 0–1 | 0 | Chance of a delay before the response |
| `slow_seconds` | 0–60 | 5 | The delay |

```bash
curl -s -X PUT http://127.0.0.1:8001/admin/faults \
  -H 'X-API-Key: dev-mock-key' -H 'Content-Type: application/json' \
  -d '{"rate_limit_rate": 0.3, "server_error_rate": 0.3, "slow_rate": 0.2, "slow_seconds": 5}'
```

Injected faults carry an `X-Fault-Injected` header (`rate_limit`, `server_error`, `server_error_after_write`)
and are logged as `fault injected`:

```http
HTTP/1.1 429 Too Many Requests
x-fault-injected: rate_limit
retry-after: 1

{"detail": "Rate limit exceeded. Retry after 1 second."}
```

```http
HTTP/1.1 503 Service Unavailable
x-fault-injected: server_error

{"detail": "Service temporarily unavailable. Retry later."}
```

The 503-after-commit is the case idempotency keys exist for: the client sees a failure, but the order is
recorded. Retrying with the same key returns that order instead of creating a second one.

Faults can also be set at startup through env vars, e.g. `API_FAULTS__SERVER_ERROR_RATE=0.3`.

## Seed data

| Supplier | Id | Approved |
|---|---|---|
| Northwind Scientific | `SUP-NW` | yes |
| BioLine Supplies | `SUP-BL` | yes |
| LabCore Direct | `SUP-LC` | yes |
| QuickChem Discount | `SUP-QC` | **no** |

13 catalogue items (12 orderable) across reagents, consumables and cell culture, and 11 stock rows, some at
zero (e.g. `LC-GLOVE-M`, `BL-FBS-500`). See `src/supplies_agent/api/seed.py`. Purchase orders are kept across
restarts in `API_DB_PATH` (default `data/mock.db`).
