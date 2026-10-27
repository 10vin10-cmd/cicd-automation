"""Payments: charges an order.

PAYMENTS_FAULT injects the failures used by the root-cause scenarios:
  schema_v2  the handler expects a renamed field (`amount` instead of
             `amount_cents`), as a careless refactor would; callers get 500
  slow       every charge takes 2.5 s, longer than callers wait
"""

from __future__ import annotations

import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from shared import telemetry

SERVICE = "payments"
log = telemetry.setup(SERVICE)
FAULT = os.environ.get("PAYMENTS_FAULT", "")
SUPPORTED_CURRENCIES = {"INR", "USD", "EUR"}

app = FastAPI(title="payments")
telemetry.instrument(app, SERVICE)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc,
              extra={"http_status": 500})
    return JSONResponse({"error": "internal error"}, status_code=500)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/charges", status_code=201)
def charge(body: dict):
    if FAULT == "slow":
        time.sleep(2.5)

    amount = body["amount"] if FAULT == "schema_v2" else body["amount_cents"]
    currency = body.get("currency", "INR")
    if currency not in SUPPORTED_CURRENCIES:
        return JSONResponse({"error": f"unsupported currency {currency}"}, status_code=422)
    if not isinstance(amount, int) or amount <= 0:
        return JSONResponse({"error": "amount_cents must be a positive integer"}, status_code=422)

    charge_id = "ch_" + uuid.uuid4().hex[:12]
    log.info("charged", extra={"order_id": body.get("order_id")})
    return {"charge_id": charge_id, "status": "captured", "amount_cents": amount,
            "currency": currency}
