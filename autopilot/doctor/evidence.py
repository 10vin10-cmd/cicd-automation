"""Load everything a pipeline run left behind into one bundle.

Layout of an evidence directory (the pipeline and `autopilot stack` write it):

    stages/<name>.json     one pipeline stage each: name, kind, status, service, log
                           (stages.json, a list, is also accepted)
    junit/*.xml            test reports (`<suite>.retry.xml` for the retry)
    traces/<service>.jsonl spans (OpenTelemetry fields, one per line)
    logs/<service>.jsonl   structured logs carrying trace ids
    process/<service>.log  raw process output (start-up crashes land here)
    stack.json             which services started, crashed or never got healthy
    changes.json           files and commits per service since the last good build
    deploys.json           deploy history with outcomes
    build/*.log            build output

Every object keeps where it came from (file and line), so evidence quoted in a
diagnosis can be checked by a person in seconds.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from autopilot.registry import Registry


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    name: str
    kind: str
    start_ns: int
    end_ns: int
    status: str
    attributes: dict
    events: list[dict]
    source: str

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6

    @property
    def http_status(self) -> int | None:
        value = self.attributes.get("http.response.status_code",
                                    self.attributes.get("http.status_code"))
        return int(value) if value is not None else None

    @property
    def url(self) -> str | None:
        return self.attributes.get("url.full") or self.attributes.get("http.url")

    @property
    def peer_port(self) -> int | None:
        if port := self.attributes.get("server.port") or self.attributes.get("net.peer.port"):
            return int(port)
        return urlparse(self.url).port if self.url else None

    @property
    def exception(self) -> dict | None:
        for event in self.events:
            if event.get("name") == "exception":
                a = event.get("attributes", {})
                return {"type": a.get("exception.type", ""),
                        "message": a.get("exception.message", ""),
                        "stacktrace": a.get("exception.stacktrace", "")}
        return None

    @property
    def is_error(self) -> bool:
        """A server-side failure. OpenTelemetry marks client spans as errors
        for any 4xx, but a 404 or 409 is a correct answer, not a failure."""
        status = self.http_status
        if status is not None:
            return status >= 500
        return self.status == "ERROR" or self.exception is not None


@dataclass
class LogLine:
    service: str
    level: str
    message: str
    trace_id: str | None
    raw: dict
    source: str


@dataclass
class TestCase:
    id: str
    suite: str
    outcome: str            # passed | failed | error | skipped
    message: str
    details: str
    duration_s: float
    passed_on_retry: bool = False
    source: str = ""


@dataclass
class Stage:
    name: str
    kind: str               # build | unit | integration | deploy | merge
    status: str             # success | failure | skipped
    service: str | None = None
    log: str | None = None
    summary: str = ""


@dataclass
class Bundle:
    root: Path
    registry: Registry
    stages: list[Stage] = field(default_factory=list)
    tests: list[TestCase] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    logs: list[LogLine] = field(default_factory=list)
    process_output: dict[str, str] = field(default_factory=dict)
    stack: dict | None = None
    changes: dict | None = None
    deploys: list[dict] = field(default_factory=list)

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, root: Path | str, registry: Registry | None = None) -> Bundle:
        root = Path(root)
        b = cls(root=root, registry=registry or Registry.load())
        if (f := root / "stages.json").exists():
            b.stages = [Stage(**s) for s in json.loads(f.read_text())]
        for f in sorted((root / "stages").glob("*.json")):
            b.stages.append(Stage(**json.loads(f.read_text())))
        b.tests = _load_junit(root / "junit")
        for f in sorted((root / "traces").glob("*.jsonl")):
            b.spans.extend(_load_spans(f, root))
        for f in sorted((root / "logs").glob("*.jsonl")):
            b.logs.extend(_load_logs(f, root))
        for f in sorted((root / "process").glob("*.log")):
            b.process_output[f.stem] = f.read_text(errors="replace")
        for name, attr in (("stack.json", "stack"), ("changes.json", "changes")):
            if (f := root / name).exists():
                setattr(b, attr, json.loads(f.read_text()))
        if (f := root / "deploys.json").exists():
            b.deploys = json.loads(f.read_text())
        return b

    # -- queries -------------------------------------------------------------

    def traces(self) -> dict[str, list[Span]]:
        out: dict[str, list[Span]] = {}
        for span in self.spans:
            out.setdefault(span.trace_id, []).append(span)
        return out

    def failed_stages(self) -> list[Stage]:
        return [s for s in self.stages if s.status == "failure"]

    def failed_tests(self) -> list[TestCase]:
        return [t for t in self.tests if t.outcome in ("failed", "error")]

    def flaky_tests(self) -> list[TestCase]:
        return [t for t in self.tests if t.passed_on_retry]

    def commits_for(self, service: str) -> list[dict]:
        if not self.changes:
            return []
        return list(self.changes.get("commits", {}).get(service, []))

    def peer_service(self, span: Span) -> str | None:
        port = span.peer_port
        return self.registry.service_for_port(port) if port else None

    def read_log(self, relpath: str) -> str:
        path = (self.root / relpath).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("log path escapes the evidence directory")
        return path.read_text(errors="replace")


# -- parsers -----------------------------------------------------------------

def _load_spans(path: Path, root: Path) -> list[Span]:
    spans = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        d = json.loads(line)
        spans.append(Span(
            trace_id=d["trace_id"], span_id=d["span_id"], parent_span_id=d.get("parent_span_id"),
            service=d.get("service") or path.stem, name=d["name"], kind=d["kind"],
            start_ns=d["start_ns"], end_ns=d["end_ns"], status=d["status"],
            attributes=d.get("attributes", {}), events=d.get("events", []),
            source=f"{path.relative_to(root)}:{n}",
        ))
    return spans


def _load_logs(path: Path, root: Path) -> list[LogLine]:
    lines = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        lines.append(LogLine(service=d.get("service", path.stem), level=d.get("level", ""),
                             message=d.get("message", ""), trace_id=d.get("trace_id"), raw=d,
                             source=f"{path.relative_to(root)}:{n}"))
    return lines


def _load_junit(directory: Path) -> list[TestCase]:
    if not directory.exists():
        return []
    first: dict[str, TestCase] = {}
    retried_passed: set[str] = set()
    for f in sorted(directory.glob("*.xml")):
        retry = f.stem.endswith(".retry")
        suite = f.stem.removesuffix(".retry")
        for case in ET.parse(f).getroot().iter("testcase"):
            classname = case.get("classname", "")
            test_id = f"{case.get('file') or classname.replace('.', '/') + '.py'}::{case.get('name')}"
            outcome, message, details = "passed", "", ""
            for tag in ("failure", "error", "skipped"):
                el = case.find(tag)
                if el is not None:
                    outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                    message = el.get("message", "")
                    details = (el.text or "").strip()
                    break
            if retry:
                if outcome == "passed":
                    retried_passed.add(test_id)
                continue
            first[test_id] = TestCase(id=test_id, suite=suite, outcome=outcome, message=message,
                                      details=details, duration_s=float(case.get("time", 0)),
                                      source=str(f.relative_to(directory.parent)))
    for test_id in retried_passed:
        if test_id in first:
            first[test_id].passed_on_retry = True
    return list(first.values())
