"""Build small, hand-written evidence directories for tests."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

_ids = itertools.count(1)
PORTS = {"orders": 8101, "inventory": 8102, "payments": 8103}


def _id(n=16) -> str:
    return format(next(_ids), f"0{n}x")


class EvidenceBuilder:
    def __init__(self, root: Path):
        self.root = root
        self.spans: dict[str, list[dict]] = {}
        self.t = 1_000_000_000

    def span(self, service, kind, name, trace, parent=None, status="UNSET", http=None,
             exc=None, ms=10, url=None) -> dict:
        attrs = {}
        if http is not None:
            attrs["http.response.status_code"] = http
        if url:
            attrs["url.full"] = url
        events = []
        if exc:
            etype, msg = exc
            events.append({"name": "exception", "attributes": {
                "exception.type": etype, "exception.message": msg}})
        s = {"trace_id": trace, "span_id": _id(), "parent_span_id": parent, "service": service,
             "name": name, "kind": kind, "start_ns": self.t, "end_ns": self.t + int(ms * 1e6),
             "status": status, "attributes": attrs, "events": events}
        self.t += int(ms * 1e6) + 1000
        self.spans.setdefault(service, []).append(s)
        return s

    def call(self, caller_span: dict, callee: str, path: str, **client_kw) -> dict:
        url = f"http://127.0.0.1:{PORTS[callee]}{path}"
        return self.span(caller_span["service"], "CLIENT", "POST", caller_span["trace_id"],
                         parent=caller_span["span_id"], url=url, **client_kw)

    def failed_checkout_via_payments_exception(self) -> None:
        trace = _id(32)
        root = self.span("orders", "SERVER", "POST /orders", trace, status="ERROR", http=502)
        self.call(root, "inventory", "/reservations", http=201)
        client = self.call(root, "payments", "/charges", status="ERROR", http=500)
        self.span("payments", "SERVER", "POST /charges", trace, parent=client["span_id"],
                  status="ERROR", http=500, exc=("KeyError", "'amount'"))

    def checkout_out_of_stock(self) -> None:
        trace = _id(32)
        root = self.span("orders", "SERVER", "POST /orders", trace, http=409)
        client = self.call(root, "inventory", "/reservations", status="ERROR", http=409)
        self.span("inventory", "SERVER", "POST /reservations", trace, parent=client["span_id"],
                  http=409)

    def checkout_timeout(self) -> None:
        trace = _id(32)
        root = self.span("orders", "SERVER", "POST /orders", trace, status="ERROR", http=504)
        client = self.call(root, "payments", "/charges", status="ERROR", ms=1000,
                           exc=("httpx.ReadTimeout", "timed out"))
        self.span("payments", "SERVER", "POST /charges", trace, parent=client["span_id"],
                  http=201, ms=2500)

    def checkout_refused(self) -> None:
        trace = _id(32)
        root = self.span("orders", "SERVER", "POST /orders", trace, status="ERROR", http=503)
        self.call(root, "inventory", "/reservations", status="ERROR", ms=2,
                  exc=("httpx.ConnectError", "[Errno 111] Connection refused"))

    def crashed(self, service: str, output: str) -> None:
        (self.root / "process").mkdir(parents=True, exist_ok=True)
        (self.root / "process" / f"{service}.log").write_text(output)
        stack = self._read("stack.json") or {"services": {}}
        stack["services"][service] = {"status": "crashed", "exit_code": 1, "pid": 1}
        self._write("stack.json", stack)

    def stage(self, **stage) -> None:
        stages = self._read("stages.json") or []
        stages.append(stage)
        self._write("stages.json", stages)

    def commit(self, service: str, sha: str, subject: str, files=None) -> None:
        changes = self._read("changes.json") or {"commits": {}}
        changes["commits"].setdefault(service, []).append(
            {"sha": sha, "author": "dev", "date": "2026-10-27T10:00:00+05:30", "subject": subject,
             "files": files or []})
        self._write("changes.json", changes)

    def junit(self, suite: str, cases: list[tuple[str, str | None]], retry=False) -> None:
        """cases: (test id 'path::name', failure message or None)."""
        out = ['<testsuites><testsuite name="pytest">']
        for test_id, failure in cases:
            path, name = test_id.split("::")
            classname = path.removesuffix(".py").replace("/", ".")
            out.append(f'<testcase classname="{classname}" name="{name}" time="0.1">')
            if failure:
                out.append(f'<failure message="{failure}">Traceback...\n{failure}</failure>')
            out.append("</testcase>")
        out.append("</testsuite></testsuites>")
        d = self.root / "junit"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{suite}{'.retry' if retry else ''}.xml").write_text("".join(out))

    def file(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def write(self) -> Path:
        d = self.root / "traces"
        d.mkdir(parents=True, exist_ok=True)
        for service, spans in self.spans.items():
            (d / f"{service}.jsonl").write_text("\n".join(json.dumps(s) for s in spans) + "\n")
        return self.root

    def _read(self, name):
        p = self.root / name
        return json.loads(p.read_text()) if p.exists() else None

    def _write(self, name, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(json.dumps(data))
