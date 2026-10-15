"""HTTP API of the mock inventory and ordering service."""

import hashlib
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.security import APIKeyHeader

from supplies_agent.api import db
from supplies_agent.api.faults import inject_faults
from supplies_agent.api.models import (
    CatalogItem,
    Category,
    Page,
    PurchaseOrder,
    PurchaseOrderIn,
    StockLevel,
)
from supplies_agent.api.settings import ApiSettings, FaultConfig
from supplies_agent.logs import configure_logging

MAX_PAGE_SIZE = 20

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    request: Request, api_key: Annotated[str | None, Depends(_api_key_header)]
) -> None:
    expected = request.app.state.settings.key
    if api_key is None or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
PageNumber = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/catalog/items")
def search_catalog(
    conn: Conn,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Category | None = None,
    page: PageNumber = 1,
    page_size: PageSize = 10,
) -> Page[CatalogItem]:
    return db.search_catalog(conn, q, category, page, page_size)


@router.get("/catalog/items/{sku}")
def get_catalog_item(conn: Conn, sku: str) -> CatalogItem:
    item = db.get_catalog_item(conn, sku)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"No catalogue item with SKU {sku!r}. Search the catalogue for valid SKUs.",
        )
    return item


@router.get("/stock")
def get_stock(
    conn: Conn, q: Annotated[str | None, Query(max_length=100)] = None
) -> list[StockLevel]:
    return db.find_stock(conn, q)


@router.post("/purchase-orders", status_code=201)
def create_purchase_order(
    conn: Conn,
    order: PurchaseOrderIn,
    idempotency_key: Annotated[str, Header(min_length=8, max_length=200)],
    response: Response,
) -> PurchaseOrder:
    request_hash = hashlib.sha256(order.model_dump_json().encode()).hexdigest()
    try:
        po, replayed = db.create_purchase_order(conn, idempotency_key, request_hash, order)
    except db.OrderRejected as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except db.IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if replayed:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    return po


@router.get("/purchase-orders")
def list_purchase_orders(
    conn: Conn,
    requested_by: str | None = None,
    page: PageNumber = 1,
    page_size: PageSize = 10,
) -> Page[PurchaseOrder]:
    return db.list_purchase_orders(conn, requested_by, page, page_size)


@router.get("/purchase-orders/{po_number}")
def get_purchase_order(conn: Conn, po_number: str) -> PurchaseOrder:
    po_id = db.parse_po_number(po_number)
    po = db.get_purchase_order(conn, po_id) if po_id is not None else None
    if po is None:
        raise HTTPException(
            status_code=404,
            detail=f"No purchase order {po_number!r}. PO numbers look like PO-000001.",
        )
    return po


@router.get("/admin/faults")
def get_faults(request: Request) -> FaultConfig:
    return request.app.state.faults


@router.put("/admin/faults")
def set_faults(request: Request, faults: FaultConfig) -> FaultConfig:
    request.app.state.faults = faults
    return faults


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(settings.db_path)
        yield

    app = FastAPI(title="Lab supplies mock API", lifespan=lifespan)
    app.state.settings = settings
    app.state.faults = settings.faults
    app.middleware("http")(inject_faults)
    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    configure_logging()
    settings = ApiSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
