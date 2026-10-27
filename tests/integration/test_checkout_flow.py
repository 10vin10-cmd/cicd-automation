"""End-to-end checkout against the running stack (see `autopilot stack up`).

These tests only talk to orders, the customer-facing service, the way a
customer would. When one fails, the failure is usually somewhere else: that is
what the root-cause agents work out from the evidence the stack collected.
"""

import os

import httpx
import pytest

ORDERS = os.environ.get("ORDERS_URL", "http://127.0.0.1:8101")
INVENTORY = os.environ.get("INVENTORY_URL", "http://127.0.0.1:8102")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def http():
    with httpx.Client(timeout=5.0) as client:
        yield client


def place(http, sku="SKU-1001", qty=1, price=49900):
    return http.post(f"{ORDERS}/orders", json={"sku": sku, "qty": qty, "unit_price_cents": price})


def test_order_is_confirmed_and_retrievable(http):
    r = place(http)
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["status"] == "confirmed"
    assert http.get(f"{ORDERS}/orders/{order['order_id']}").status_code == 200


def test_successful_order_reserves_stock(http):
    before = http.get(f"{INVENTORY}/stock/SKU-1001").json()["available"]
    assert place(http, qty=2).status_code == 201
    after = http.get(f"{INVENTORY}/stock/SKU-1001").json()["available"]
    assert after == before - 2


def test_out_of_stock_is_rejected(http):
    r = place(http, sku="SKU-1003")
    assert r.status_code == 409, r.text


def test_unknown_product_is_rejected(http):
    assert place(http, sku="SKU-9999").status_code == 422
