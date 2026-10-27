"""Tracing and structured logging shared by the sample services.

Spans come from the standard OpenTelemetry SDK and auto-instrumentation
(FastAPI server spans, httpx client spans). The only custom piece is the
exporter: it writes finished spans as JSON lines to a file, which is what the
pipeline collects as evidence. In production you would point an OTLP exporter
at a collector instead; the span fields are the same.

Logs are JSON lines carrying the active trace and span ids, so a log line can
be joined to the request that produced it without searching by timestamp.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


class JsonLinesSpanExporter(SpanExporter):
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock, open(self._path, "a", encoding="utf-8") as f:
            for span in spans:
                f.write(json.dumps(span_to_dict(span)) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # nothing buffered
        pass


def span_to_dict(span: ReadableSpan) -> dict:
    ctx = span.get_span_context()
    parent = span.parent
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": format(parent.span_id, "016x") if parent else None,
        "service": span.resource.attributes.get("service.name"),
        "name": span.name,
        "kind": span.kind.name,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "status": span.status.status_code.name,
        "status_message": span.status.description,
        "attributes": {k: v for k, v in (span.attributes or {}).items()},
        "events": [
            {"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events
        ],
    }


class JsonLogFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        doc = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "service": self.service,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if ctx.is_valid:
            doc["trace_id"] = format(ctx.trace_id, "032x")
            doc["span_id"] = format(ctx.span_id, "016x")
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            doc["exception"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "stack": traceback.format_exception(exc)[-6:],
            }
        for key in ("http_status", "downstream", "order_id", "sku"):
            if hasattr(record, key):
                doc[key] = getattr(record, key)
        return json.dumps(doc)


def setup(service: str) -> logging.Logger:
    """Configure tracing and logging for one service. Idempotent."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        trace_file = os.environ.get("TRACE_FILE")
        if trace_file:
            provider.add_span_processor(SimpleSpanProcessor(JsonLinesSpanExporter(trace_file)))
        trace.set_tracer_provider(provider)

    logger = logging.getLogger(service)
    if not logger.handlers:
        log_file = os.environ.get("LOG_FILE")
        handler: logging.Handler = (
            logging.FileHandler(log_file, encoding="utf-8") if log_file
            else logging.StreamHandler(sys.stdout)
        )
        handler.setFormatter(JsonLogFormatter(service))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def instrument(app, service: str) -> None:
    """Server spans for the FastAPI app and client spans for outgoing httpx calls."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    httpx_instrumentor = HTTPXClientInstrumentor()
    if not httpx_instrumentor.is_instrumented_by_opentelemetry:
        httpx_instrumentor.instrument()
