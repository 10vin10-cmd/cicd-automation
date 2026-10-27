"""Inventory: stock levels and reservations.

Configuration is read at start-up, as a real service would read its database
URL. Starting without INVENTORY_DB_URL fails immediately, which is one of the
fault scenarios the root-cause agents learn to recognise.
"""

from __future__ import annotations

import os
import threading
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared import telemetry

SERVICE = "inventory"
log = telemetry.setup(SERVICE)

DB_URL = os.environ["INVENTORY_DB_URL"]  # required configuration

app = FastAPI(title="inventory")
telemetry.instrument(app, SERVICE)

_lock = threading.Lock()
_stock: dict[str, int] = {"SKU-1001": 10_000, "SKU-1002": 3, "SKU-1003": 0}
_reservations: dict[str, dict] = {}


class ReservationRequest(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=100)
    order_id: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/stock/{sku}")
def stock(sku: str):
    if sku not in _stock:
        raise HTTPException(404, "unknown sku")
    return {"sku": sku, "available": _stock[sku]}


@app.post("/reservations", status_code=201)
def reserve(req: ReservationRequest):
    with _lock:
        available = _stock.get(req.sku)
        if available is None:
            raise HTTPException(404, "unknown sku")
        if available < req.qty:
            log.info("insufficient stock", extra={"sku": req.sku, "order_id": req.order_id})
            raise HTTPException(409, "insufficient stock")
        _stock[req.sku] = available - req.qty
        reservation_id = uuid.uuid4().hex[:12]
        _reservations[reservation_id] = req.model_dump()
    log.info("reserved", extra={"sku": req.sku, "order_id": req.order_id})
    return {"reservation_id": reservation_id, "sku": req.sku, "qty": req.qty}


@app.delete("/reservations/{reservation_id}", status_code=204)
def release(reservation_id: str):
    with _lock:
        res = _reservations.pop(reservation_id, None)
        if res:
            _stock[res["sku"]] += res["qty"]
