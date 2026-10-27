import json

import httpx
import pytest
from fastapi.testclient import TestClient

from orders import app as orders

ORDER = {"sku": "SKU-1", "qty": 2, "unit_price_cents": 500}


def stub(monkeypatch, inventory=None, payments=None):
    """Route orders' outgoing calls to fake dependencies."""
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path, request.content))
        if request.url.path.startswith("/reservations"):
            if request.method == "DELETE":
                return httpx.Response(204)
            return inventory(request) if inventory else httpx.Response(
                201, json={"reservation_id": "res1"})
        return payments(request) if payments else httpx.Response(
            201, json={"charge_id": "ch_1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(orders, "client", lambda: client)
    monkeypatch.setattr(orders, "FAULT", "")
    return TestClient(orders.app), calls


def test_places_order_and_charges_total(monkeypatch):
    api, calls = stub(monkeypatch)
    r = api.post("/orders", json=ORDER)
    assert r.status_code == 201
    assert r.json()["status"] == "confirmed"
    charge = json.loads(calls[1][2])
    assert charge["amount_cents"] == 1000


def test_out_of_stock(monkeypatch):
    api, _ = stub(monkeypatch, inventory=lambda r: httpx.Response(409))
    assert api.post("/orders", json=ORDER).status_code == 409


def test_payment_failure_releases_reservation_and_names_dependency(monkeypatch):
    api, calls = stub(monkeypatch, payments=lambda r: httpx.Response(500))
    r = api.post("/orders", json=ORDER)
    assert r.status_code == 502
    assert r.json()["downstream"] == "payments"
    assert ("DELETE", "/reservations/res1", b"") in calls


def test_payment_timeout_is_504(monkeypatch):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    api, _ = stub(monkeypatch, payments=slow)
    assert api.post("/orders", json=ORDER).status_code == 504


def test_inventory_down_is_503(monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    api, _ = stub(monkeypatch, inventory=down)
    r = api.post("/orders", json=ORDER)
    assert r.status_code == 503
    assert r.json()["downstream"] == "inventory"


@pytest.mark.parametrize("bad", [{"qty": 0}, {"unit_price_cents": 0}, {"sku": None}])
def test_validates_input(monkeypatch, bad):
    api, calls = stub(monkeypatch)
    assert api.post("/orders", json={**ORDER, **bad}).status_code == 422
    assert calls == []
