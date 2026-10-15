import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from supplies_agent.api.app import create_app
from supplies_agent.api.settings import ApiSettings

API_KEY = "test-key"
AUTH = {"X-API-Key": API_KEY}
ORDER = {
    "requested_by": "alice",
    "deliver_to": "Lab 3.12",
    "lines": [{"sku": "LC-GLOVE-M", "quantity": 2}],
}


@pytest.fixture
def settings(tmp_path) -> ApiSettings:
    return ApiSettings(key=API_KEY, db_path=str(tmp_path / "mock.db"))


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def create_po(client: TestClient, key: str, order: dict = ORDER):
    return client.post("/purchase-orders", json=order, headers={**AUTH, "Idempotency-Key": key})


def set_faults(client: TestClient, **faults):
    client.put("/admin/faults", json=faults, headers=AUTH).raise_for_status()


# --- Auth ------------------------------------------------------------------


def test_health_needs_no_key(client):
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_api_rejects_missing_or_wrong_key(client, headers):
    response = client.get("/stock", headers=headers)
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["detail"]


# --- Catalogue and stock ---------------------------------------------------


def test_search_paginates_and_hides_unapproved_suppliers(client):
    first = client.get("/catalog/items", params={"page_size": 5}, headers=AUTH).json()
    assert first["total"] == 12  # 13 seeded items, one from the unapproved supplier
    assert first["total_pages"] == 3
    assert len(first["items"]) == 5

    last = client.get("/catalog/items", params={"page": 3, "page_size": 5}, headers=AUTH).json()
    assert len(last["items"]) == 2


def test_search_by_text_and_category(client):
    ethanol = client.get("/catalog/items", params={"q": "ethanol"}, headers=AUTH).json()
    assert [i["sku"] for i in ethanol["items"]] == ["NW-ETOH-2L5"]

    cell = client.get("/catalog/items", params={"category": "cell_culture"}, headers=AUTH).json()
    assert {i["category"] for i in cell["items"]} == {"cell_culture"}


def test_page_size_is_capped(client):
    response = client.get("/catalog/items", params={"page_size": 50}, headers=AUTH)
    assert response.status_code == 422


def test_get_item_from_unapproved_supplier_is_not_found(client):
    assert client.get("/catalog/items/NW-ETOH-2L5", headers=AUTH).status_code == 200
    assert client.get("/catalog/items/QC-ETOH-2L5", headers=AUTH).status_code == 404


def test_stock_matches_name_or_sku(client):
    by_name = client.get("/stock", params={"q": "gloves"}, headers=AUTH).json()
    assert by_name == [
        {
            "sku": "LC-GLOVE-M",
            "name": "Nitrile gloves, size M",
            "location": "Lab 3.12 shelf",
            "quantity": 0,
            "pack_size": "100 gloves",
        }
    ]
    assert client.get("/stock", params={"q": "BL-PBS"}, headers=AUTH).json()[0]["quantity"] == 6


# --- Purchase orders -------------------------------------------------------


def test_create_and_read_purchase_order(client):
    response = create_po(client, "key-00000001")
    assert response.status_code == 201
    po = response.json()
    assert po["po_number"] == "PO-000001"
    assert po["supplier_id"] == "SUP-LC"
    assert po["total_cents"] == 2 * 1200

    assert client.get("/purchase-orders/PO-000001", headers=AUTH).json() == po


def test_same_key_same_order_is_replayed_not_duplicated(client):
    first = create_po(client, "key-00000001")
    again = create_po(client, "key-00000001")

    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json()
    assert client.get("/purchase-orders", headers=AUTH).json()["total"] == 1


def test_same_key_different_order_conflicts(client):
    create_po(client, "key-00000001")
    other = {
        "requested_by": "alice",
        "deliver_to": "Lab 3.12",
        "lines": [{"sku": "LC-GLOVE-M", "quantity": 5}],
    }
    assert create_po(client, "key-00000001", other).status_code == 409


@pytest.mark.parametrize("deliver_to", [None, ""])
def test_delivery_location_is_required(client, deliver_to):
    order = {k: v for k, v in ORDER.items() if k != "deliver_to"}
    if deliver_to is not None:
        order["deliver_to"] = deliver_to
    assert create_po(client, "key-00000001", order).status_code == 422
    assert client.get("/purchase-orders", headers=AUTH).json()["total"] == 0


def test_idempotency_key_is_required(client):
    response = client.post("/purchase-orders", json=ORDER, headers=AUTH)
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([{"sku": "NOPE", "quantity": 1}], "Unknown SKU"),
        ([{"sku": "QC-ETOH-2L5", "quantity": 1}], "Unknown SKU"),
        (
            [{"sku": "LC-GLOVE-M", "quantity": 1}, {"sku": "BL-PBS-500", "quantity": 1}],
            "one purchase order per supplier",
        ),
        (
            [{"sku": "LC-GLOVE-M", "quantity": 1}, {"sku": "LC-GLOVE-M", "quantity": 1}],
            "only once per order",
        ),
    ],
)
def test_invalid_orders_are_rejected_with_guidance(client, lines, message):
    response = create_po(
        client, "key-00000001", {"requested_by": "alice", "deliver_to": "Lab 3.12", "lines": lines}
    )
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert client.get("/purchase-orders", headers=AUTH).json()["total"] == 0


def test_list_purchase_orders_filters_by_requester(client):
    create_po(client, "key-00000001")
    create_po(client, "key-00000002", {**ORDER, "requested_by": "bob"})
    page = client.get("/purchase-orders", params={"requested_by": "bob"}, headers=AUTH).json()
    assert [po["requested_by"] for po in page["items"]] == ["bob"]


def test_purchase_orders_survive_restart(settings):
    with TestClient(create_app(settings)) as c:
        create_po(c, "key-00000001")
    with TestClient(create_app(settings)) as c:
        assert c.get("/purchase-orders/PO-000001", headers=AUTH).status_code == 200
        assert c.get("/catalog/items", headers=AUTH).json()["total"] == 12  # seed not duplicated


# --- Fault injection -------------------------------------------------------


def test_rate_limit_fault(client):
    set_faults(client, rate_limit_rate=1)
    response = client.get("/stock", headers=AUTH)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.headers["X-Fault-Injected"] == "rate_limit"


def test_admin_and_health_are_never_faulted(client):
    set_faults(client, rate_limit_rate=1, server_error_rate=1)
    assert client.get("/health").status_code == 200
    assert client.get("/admin/faults", headers=AUTH).status_code == 200


def test_server_error_on_write_happens_after_commit_and_retry_is_safe(client):
    set_faults(client, server_error_rate=1)
    failed = create_po(client, "key-00000001")
    assert failed.status_code == 503
    assert failed.headers["X-Fault-Injected"] == "server_error_after_write"

    set_faults(client)  # faults off
    assert client.get("/purchase-orders", headers=AUTH).json()["total"] == 1  # the write landed

    retry = create_po(client, "key-00000001")
    assert retry.status_code == 200
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert client.get("/purchase-orders", headers=AUTH).json()["total"] == 1


def test_injected_faults_are_logged(client, caplog):
    caplog.set_level(logging.INFO, logger="supplies_agent")
    set_faults(client, rate_limit_rate=1)
    client.get("/stock", headers=AUTH)
    assert "fault=<rate_limit>, method=<GET>, path=</stock> | fault injected" in caplog.text
