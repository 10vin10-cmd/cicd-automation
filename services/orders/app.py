"""Orders: the customer-facing service. Places an order by reserving stock in
inventory, then charging it in payments. When either dependency fails, orders
returns an error that names the dependency, and releases the reservation if
the charge failed.

ORDERS_FAULT=flaky_warmup makes the first request after start-up fail with
503, a classic warm-up race that makes one test fail and pass on retry.
"""

from __future__ import annotations

import functools
import os
import threading
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared import telemetry

SERVICE = "orders"
log = telemetry.setup(SERVICE)

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://127.0.0.1:8102")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://127.0.0.1:8103")
PAYMENTS_TIMEOUT_S = float(os.environ.get("PAYMENTS_TIMEOUT_S", "1.0"))
FAULT = os.environ.get("ORDERS_FAULT", "")

app = FastAPI(title="orders")
telemetry.instrument(app, SERVICE)

_orders: dict[str, dict] = {}
_warm = threading.Event()


class OrderRequest(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=100)
    unit_price_cents: int = Field(gt=0)
    currency: str = "INR"


@functools.cache
def client() -> httpx.Client:
    # Created after instrumentation so outgoing calls get client spans.
    return httpx.Client(timeout=httpx.Timeout(2.0, read=PAYMENTS_TIMEOUT_S))


def _fail(status: int, error: str, downstream: str, **extra):
    log.warning(error, extra={"http_status": status, "downstream": downstream, **extra})
    return JSONResponse({"error": error, "downstream": downstream}, status_code=status)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/orders", status_code=201)
def place_order(req: OrderRequest):
    if FAULT == "flaky_warmup" and not _warm.is_set():
        _warm.set()
        return JSONResponse({"error": "warming up"}, status_code=503)

    order_id = "ord_" + uuid.uuid4().hex[:12]

    try:
        r = client().post(f"{INVENTORY_URL}/reservations",
                          json={"sku": req.sku, "qty": req.qty, "order_id": order_id})
    except httpx.TransportError as exc:
        return _fail(503, f"inventory unavailable: {type(exc).__name__}", "inventory",
                     order_id=order_id)
    if r.status_code == 409:
        return JSONResponse({"error": "out of stock"}, status_code=409)
    if r.status_code == 404:
        return JSONResponse({"error": "unknown sku"}, status_code=422)
    if r.status_code >= 400:
        return _fail(502, f"inventory error {r.status_code}", "inventory", order_id=order_id)
    reservation_id = r.json()["reservation_id"]

    try:
        p = client().post(f"{PAYMENTS_URL}/charges", json={
            "order_id": order_id,
            "amount_cents": req.qty * req.unit_price_cents,
            "currency": req.currency,
        })
    except httpx.TimeoutException:
        _release(reservation_id)
        return _fail(504, "payments timed out", "payments", order_id=order_id)
    except httpx.TransportError as exc:
        _release(reservation_id)
        return _fail(503, f"payments unavailable: {type(exc).__name__}", "payments",
                     order_id=order_id)
    if p.status_code >= 400:
        _release(reservation_id)
        status = 422 if p.status_code == 422 else 502
        return _fail(status, f"payments error {p.status_code}", "payments", order_id=order_id)

    order = {"order_id": order_id, "status": "confirmed", "sku": req.sku, "qty": req.qty,
             "charge_id": p.json()["charge_id"], "reservation_id": reservation_id}
    _orders[order_id] = order
    log.info("order confirmed", extra={"order_id": order_id, "sku": req.sku})
    return order


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    if order_id not in _orders:
        raise HTTPException(404, "order not found")
    return _orders[order_id]


def _release(reservation_id: str) -> None:
    try:
        client().delete(f"{INVENTORY_URL}/reservations/{reservation_id}")
    except httpx.HTTPError:
        log.warning("could not release reservation", extra={"downstream": "inventory"})
