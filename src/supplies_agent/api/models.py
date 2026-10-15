from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["reagent", "consumable", "cell_culture"]


class Page[T](BaseModel):
    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int


class CatalogItem(BaseModel):
    sku: str
    name: str
    category: Category
    pack_size: str
    unit_price_cents: int
    currency: str
    lead_time_days: int
    supplier_id: str
    supplier_name: str


class StockLevel(BaseModel):
    sku: str
    name: str
    location: str
    quantity: int
    pack_size: str


class PurchaseOrderLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1)
    quantity: int = Field(gt=0, le=1000, description="Number of packs.")


class PurchaseOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_by: str = Field(min_length=1, description="User id of the requester.")
    deliver_to: str = Field(min_length=1, max_length=200, description="Delivery location.")
    lines: list[PurchaseOrderLineIn] = Field(min_length=1, max_length=20)


class PurchaseOrderLine(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int


class PurchaseOrder(BaseModel):
    po_number: str
    status: Literal["submitted"]
    supplier_id: str
    supplier_name: str
    requested_by: str
    deliver_to: str | None  # required on new orders; older ones may lack it
    lines: list[PurchaseOrderLine]
    total_cents: int
    currency: str
    created_at: str
