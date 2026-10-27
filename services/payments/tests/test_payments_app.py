import pytest
from fastapi.testclient import TestClient

from payments import app as payments


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(payments, "FAULT", "")
    return TestClient(payments.app)


def test_charge_captures(api):
    r = api.post("/charges", json={"order_id": "o1", "amount_cents": 1250, "currency": "INR"})
    assert r.status_code == 201
    assert r.json()["status"] == "captured"
    assert r.json()["charge_id"].startswith("ch_")


def test_rejects_unsupported_currency(api):
    r = api.post("/charges", json={"order_id": "o1", "amount_cents": 100, "currency": "XYZ"})
    assert r.status_code == 422


@pytest.mark.parametrize("amount", [0, -5, "100", 1.5])
def test_rejects_bad_amounts(api, amount):
    assert api.post("/charges", json={"order_id": "o1", "amount_cents": amount}).status_code == 422


def test_schema_fault_breaks_existing_callers(monkeypatch):
    # Guards the contract: the injected fault is what a field rename does.
    monkeypatch.setattr(payments, "FAULT", "schema_v2")
    api = TestClient(payments.app, raise_server_exceptions=False)
    assert api.post("/charges", json={"order_id": "o1", "amount_cents": 100}).status_code == 500
