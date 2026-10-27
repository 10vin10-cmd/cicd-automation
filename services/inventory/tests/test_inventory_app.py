import pytest
from fastapi.testclient import TestClient

from inventory import app as inventory


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(inventory, "_stock", {"SKU-1": 5, "SKU-EMPTY": 0})
    monkeypatch.setattr(inventory, "_reservations", {})
    return TestClient(inventory.app)


def test_reserve_decrements_stock(api):
    r = api.post("/reservations", json={"sku": "SKU-1", "qty": 2, "order_id": "o1"})
    assert r.status_code == 201
    assert api.get("/stock/SKU-1").json()["available"] == 3


def test_insufficient_stock_is_a_conflict(api):
    r = api.post("/reservations", json={"sku": "SKU-EMPTY", "qty": 1, "order_id": "o1"})
    assert r.status_code == 409


def test_unknown_sku(api):
    assert api.post("/reservations", json={"sku": "nope", "qty": 1, "order_id": "o"}).status_code == 404


def test_release_returns_stock(api):
    rid = api.post("/reservations", json={"sku": "SKU-1", "qty": 2, "order_id": "o1"}).json()[
        "reservation_id"]
    assert api.delete(f"/reservations/{rid}").status_code == 204
    assert api.get("/stock/SKU-1").json()["available"] == 5


@pytest.mark.parametrize("qty", [0, -1, 101])
def test_quantity_bounds(api, qty):
    assert api.post("/reservations", json={"sku": "SKU-1", "qty": qty, "order_id": "o"}).status_code == 422
