"""SQLite schema, seeding and queries for the mock."""

import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from supplies_agent.api import seed
from supplies_agent.api.models import (
    CatalogItem,
    Page,
    PurchaseOrder,
    PurchaseOrderIn,
    PurchaseOrderLine,
    StockLevel,
)

CURRENCY = "EUR"

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    approved  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_items (
    sku               TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    category          TEXT NOT NULL,
    supplier_id       TEXT NOT NULL REFERENCES suppliers(id),
    pack_size         TEXT NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    lead_time_days    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stock (
    sku       TEXT NOT NULL REFERENCES catalog_items(sku),
    location  TEXT NOT NULL,
    quantity  INTEGER NOT NULL,
    PRIMARY KEY (sku, location)
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT NOT NULL UNIQUE,
    request_hash     TEXT NOT NULL,
    supplier_id      TEXT NOT NULL REFERENCES suppliers(id),
    requested_by     TEXT NOT NULL,
    deliver_to       TEXT,
    total_cents      INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_order_lines (
    po_id             INTEGER NOT NULL REFERENCES purchase_orders(id),
    sku               TEXT NOT NULL REFERENCES catalog_items(sku),
    quantity          INTEGER NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    PRIMARY KEY (po_id, sku)
);
"""


class OrderRejected(Exception):
    """The order is invalid; the message tells the caller how to fix it."""


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different order."""


def connect(db_path: str) -> sqlite3.Connection:
    # Autocommit mode: writes manage their own transactions with BEGIN IMMEDIATE.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    """Create the schema and seed reference data. Purchase orders are kept across restarts."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT OR IGNORE INTO suppliers VALUES (?, ?, ?)", seed.SUPPLIERS)
        conn.executemany(
            "INSERT OR IGNORE INTO catalog_items VALUES (?, ?, ?, ?, ?, ?, ?)", seed.CATALOG_ITEMS
        )
        conn.executemany("INSERT OR IGNORE INTO stock VALUES (?, ?, ?)", seed.STOCK)
    finally:
        conn.close()


# --- Catalogue and stock -----------------------------------------------------

_CATALOG_SELECT = """
    SELECT c.sku, c.name, c.category, c.pack_size, c.unit_price_cents, c.lead_time_days,
           c.supplier_id, s.name AS supplier_name
    FROM catalog_items c JOIN suppliers s ON s.id = c.supplier_id
"""


def _catalog_item(row: sqlite3.Row) -> CatalogItem:
    return CatalogItem(**dict(row), currency=CURRENCY)


def search_catalog(
    conn: sqlite3.Connection, q: str | None, category: str | None, page: int, page_size: int
) -> Page[CatalogItem]:
    """Search items from approved suppliers by name or SKU (case-insensitive substring)."""
    where, params = ["s.approved = 1"], []
    if q:
        where.append("(c.name LIKE ? OR c.sku LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("c.category = ?")
        params.append(category)
    where_sql = " WHERE " + " AND ".join(where)

    total = conn.execute(
        "SELECT COUNT(*) FROM catalog_items c JOIN suppliers s ON s.id = c.supplier_id" + where_sql,
        params,
    ).fetchone()[0]
    rows = conn.execute(
        _CATALOG_SELECT + where_sql + " ORDER BY c.sku LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return Page[CatalogItem](
        items=[_catalog_item(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size),
    )


def get_catalog_item(conn: sqlite3.Connection, sku: str) -> CatalogItem | None:
    """Return an item only if its supplier is approved."""
    row = conn.execute(_CATALOG_SELECT + " WHERE c.sku = ? AND s.approved = 1", (sku,)).fetchone()
    return _catalog_item(row) if row else None


def find_stock(conn: sqlite3.Connection, q: str | None) -> list[StockLevel]:
    """Stock on hand, matched on item name or SKU. No query returns everything."""
    sql = """
        SELECT st.sku, c.name, st.location, st.quantity, c.pack_size
        FROM stock st JOIN catalog_items c ON c.sku = st.sku
    """
    params: list[str] = []
    if q:
        sql += " WHERE c.name LIKE ? OR st.sku LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    rows = conn.execute(sql + " ORDER BY st.sku, st.location", params).fetchall()
    return [StockLevel(**dict(r)) for r in rows]


# --- Purchase orders ---------------------------------------------------------


def format_po_number(po_id: int) -> str:
    return f"PO-{po_id:06d}"


def parse_po_number(po_number: str) -> int | None:
    prefix, _, digits = po_number.partition("-")
    return int(digits) if prefix == "PO" and digits.isdigit() else None


def create_purchase_order(
    conn: sqlite3.Connection, idempotency_key: str, request_hash: str, order: PurchaseOrderIn
) -> tuple[PurchaseOrder, bool]:
    """Create a PO, or return the existing one for a repeated idempotency key.

    Returns (purchase_order, replayed). The check and the insert run in one
    BEGIN IMMEDIATE transaction, so concurrent retries cannot both insert.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id, request_hash FROM purchase_orders WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict(
                    "This Idempotency-Key was already used for a different order. "
                    "Use a new key for a new order."
                )
            po_id, replayed = existing["id"], True
        else:
            po_id, replayed = (
                _insert_purchase_order(conn, idempotency_key, request_hash, order),
                False,
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    po = get_purchase_order(conn, po_id)
    assert po is not None
    return po, replayed


def _insert_purchase_order(
    conn: sqlite3.Connection, idempotency_key: str, request_hash: str, order: PurchaseOrderIn
) -> int:
    skus = [line.sku for line in order.lines]
    if len(set(skus)) != len(skus):
        raise OrderRejected("Each SKU may appear only once per order. Combine the quantities.")

    placeholders = ",".join("?" * len(skus))
    items = {
        row["sku"]: row
        for row in conn.execute(
            "SELECT c.sku, c.supplier_id, c.unit_price_cents, s.approved"
            " FROM catalog_items c JOIN suppliers s ON s.id = c.supplier_id"
            f" WHERE c.sku IN ({placeholders})",
            skus,
        )
    }
    # Items from unapproved suppliers are reported as unknown, same as in the catalogue.
    unknown = [sku for sku in skus if sku not in items or not items[sku]["approved"]]
    if unknown:
        raise OrderRejected(
            f"Unknown SKU(s): {', '.join(unknown)}. Search the catalogue for valid SKUs."
        )
    suppliers = sorted({items[sku]["supplier_id"] for sku in skus})
    if len(suppliers) > 1:
        raise OrderRejected(
            f"Lines span several suppliers ({', '.join(suppliers)}). "
            "Create one purchase order per supplier."
        )

    total = sum(items[line.sku]["unit_price_cents"] * line.quantity for line in order.lines)
    po_id = conn.execute(
        "INSERT INTO purchase_orders"
        " (idempotency_key, request_hash, supplier_id, requested_by, deliver_to, total_cents,"
        "  created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            idempotency_key,
            request_hash,
            suppliers[0],
            order.requested_by,
            order.deliver_to,
            total,
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    ).lastrowid
    conn.executemany(
        "INSERT INTO purchase_order_lines VALUES (?, ?, ?, ?)",
        [
            (po_id, line.sku, line.quantity, items[line.sku]["unit_price_cents"])
            for line in order.lines
        ],
    )
    return po_id


def get_purchase_order(conn: sqlite3.Connection, po_id: int) -> PurchaseOrder | None:
    po = conn.execute(
        "SELECT p.*, s.name AS supplier_name FROM purchase_orders p"
        " JOIN suppliers s ON s.id = p.supplier_id WHERE p.id = ?",
        (po_id,),
    ).fetchone()
    if po is None:
        return None
    lines = conn.execute(
        "SELECT l.sku, c.name, l.quantity, l.unit_price_cents,"
        " l.quantity * l.unit_price_cents AS line_total_cents"
        " FROM purchase_order_lines l JOIN catalog_items c ON c.sku = l.sku"
        " WHERE l.po_id = ? ORDER BY l.sku",
        (po_id,),
    ).fetchall()
    return PurchaseOrder(
        po_number=format_po_number(po["id"]),
        status="submitted",
        supplier_id=po["supplier_id"],
        supplier_name=po["supplier_name"],
        requested_by=po["requested_by"],
        deliver_to=po["deliver_to"],
        lines=[PurchaseOrderLine(**dict(r)) for r in lines],
        total_cents=po["total_cents"],
        currency=CURRENCY,
        created_at=po["created_at"],
    )


def list_purchase_orders(
    conn: sqlite3.Connection, requested_by: str | None, page: int, page_size: int
) -> Page[PurchaseOrder]:
    where, params = "", []
    if requested_by:
        where, params = " WHERE requested_by = ?", [requested_by]
    total = conn.execute("SELECT COUNT(*) FROM purchase_orders" + where, params).fetchone()[0]
    ids = conn.execute(
        "SELECT id FROM purchase_orders" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return Page[PurchaseOrder](
        items=[get_purchase_order(conn, row["id"]) for row in ids],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size),
    )
