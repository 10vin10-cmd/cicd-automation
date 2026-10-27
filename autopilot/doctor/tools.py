"""The investigation tools agents call, and the ledger of evidence they cite.

Every tool is read-only and works on the evidence bundle; nothing touches a
live system. Each item a tool returns is registered in the ledger under an ID
(E1, E2, ...) with its source file and line. A diagnosis may only cite IDs
the ledger issued, which is how made-up evidence is rejected.

Results are summaries, not raw dumps: error chains instead of span lists, log
*patterns* with counts instead of log lines. That keeps an LLM's context
small and makes the same tools usable by the offline playbook.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from autopilot.doctor import analysis
from autopilot.doctor.analysis import CATEGORIES
from autopilot.doctor.evidence import Bundle


@dataclass
class EvidenceItem:
    id: str
    summary: str
    source: str


class Ledger:
    def __init__(self) -> None:
        self.items: dict[str, EvidenceItem] = {}
        self._by_key: dict[tuple[str, str], str] = {}

    def cite(self, summary: str, source: str) -> str:
        key = (summary, source)
        if key in self._by_key:
            return self._by_key[key]
        eid = f"E{len(self.items) + 1}"
        self.items[eid] = EvidenceItem(eid, summary, source)
        self._by_key[key] = eid
        return eid

    def __contains__(self, eid: str) -> bool:
        return eid in self.items


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict              # JSON schema for the input
    fn: Any = field(repr=False)

    def schema(self) -> dict:
        return {"type": "object", "properties": self.parameters.get("properties", {}),
                "required": self.parameters.get("required", [])}


SUBMIT = "submit_diagnosis"


class Toolbox:
    def __init__(self, bundle: Bundle):
        self.bundle = bundle
        self.ledger = Ledger()
        svc_enum = sorted(bundle.registry.services)
        svc = {"type": "string", "enum": svc_enum}
        self.tools: dict[str, Tool] = {t.name: t for t in [
            Tool("pipeline_overview",
                 "Which pipeline stages ran and failed, which tests failed or were flaky, and "
                 "which services started. Call this first.",
                 {}, self.pipeline_overview),
            Tool("get_test_failure",
                 "Assertion message and traceback for one failed test.",
                 {"properties": {"test_id": {"type": "string"}}, "required": ["test_id"]},
                 self.get_test_failure),
            Tool("trace_errors",
                 "Failed requests reconstructed from distributed traces: for each failure, the "
                 "chain of services from the entry point to the deepest failing call, and what "
                 "failed there. The deepest service is where the failure started; services "
                 "above it are symptoms. Optionally filter to chains involving one service.",
                 {"properties": {"service": svc}}, self.trace_errors),
            Tool("get_trace",
                 "One trace as an indented span tree with durations, statuses and exceptions.",
                 {"properties": {"trace_id": {"type": "string"}}, "required": ["trace_id"]},
                 self.get_trace),
            Tool("service_dependencies",
                 "What a service calls and what calls it, from the service registry.",
                 {"properties": {"service": svc}, "required": ["service"]},
                 self.service_dependencies),
            Tool("log_patterns",
                 "Warning and error log lines for a service, grouped into patterns with counts "
                 "and one example each (ids, numbers and hex are masked when grouping).",
                 {"properties": {"service": svc}, "required": ["service"]}, self.log_patterns),
            Tool("process_status",
                 "Whether each service process started, and the end of its output if it "
                 "crashed or never became healthy.",
                 {}, self.process_status),
            Tool("recent_changes",
                 "Commits since the last good build that touched a service's code (or shared "
                 "code), newest first.",
                 {"properties": {"service": svc}, "required": ["service"]}, self.recent_changes),
            Tool("build_log",
                 "The relevant part of a failed build stage's log.",
                 {"properties": {"stage": {"type": "string"}}, "required": ["stage"]},
                 self.build_log),
            Tool("deploy_history",
                 "Recent deployments and their outcomes (promoted, rolled back, failed).",
                 {}, self.deploy_history),
            Tool("rank_root_causes",
                 "The deterministic analyser's ranked hypotheses. Useful as a starting point; "
                 "verify the top one with the other tools before submitting.",
                 {}, self.rank_root_causes),
            Tool(SUBMIT,
                 "Submit the final diagnosis. Call exactly once, when the evidence supports a "
                 "conclusion. Every evidence ID must have been returned by a tool.",
                 {"properties": {
                     "root_cause_service": {"type": "string", "enum": svc_enum + ["unknown"]},
                     "category": {"type": "string", "enum": list(CATEGORIES)},
                     "summary": {"type": "string",
                                 "description": "One or two sentences a release engineer can act on."},
                     "impact_path": {"type": "array", "items": {"type": "string"},
                                     "description": "Services from the entry point to the root cause."},
                     "evidence": {"type": "array", "items": {"type": "string"},
                                  "description": "Evidence IDs (E1, E2, ...) supporting the conclusion."},
                     "suspect_commits": {"type": "array", "items": {"type": "string"}},
                     "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                     "actions": {"type": "array", "items": {"type": "string"}},
                 }, "required": ["root_cause_service", "category", "summary", "evidence",
                                 "confidence", "actions"]},
                 None),
        ]}

    def specs(self, names: list[str] | None = None) -> list[Tool]:
        return [t for n, t in self.tools.items() if names is None or n in names]

    def call(self, name: str, args: dict) -> dict:
        tool = self.tools.get(name)
        if tool is None or tool.fn is None:
            raise KeyError(f"unknown tool {name}")
        args = args or {}
        service = args.get("service")
        if service is not None and service not in self.bundle.registry.services:
            # Models don't always respect the schema's enum; a silent empty
            # answer for a misspelled service would mislead the investigation.
            return {"error": f"unknown service {service!r}; known services: "
                             f"{sorted(self.bundle.registry.services)}"}
        return tool.fn(**args)

    # -- tools ---------------------------------------------------------------

    def pipeline_overview(self) -> dict:
        b = self.bundle
        stages = [{"stage": s.name, "kind": s.kind, "status": s.status, "service": s.service,
                   "evidence": self.ledger.cite(f"stage {s.name}: {s.status}", "stages.json")}
                  for s in b.stages]
        failed = [{"test": t.id, "message": t.message[:200],
                   "evidence": self.ledger.cite(f"test failed: {t.id}: {t.message[:120]}", t.source)}
                  for t in b.failed_tests() if not t.passed_on_retry]
        flaky = [{"test": t.id, "first_failure": t.message[:160],
                  "evidence": self.ledger.cite(f"flaky: {t.id} failed then passed on retry",
                                               t.source)}
                 for t in b.flaky_tests()]
        procs = {name: info.get("status") for name, info in (b.stack or {}).get("services", {}).items()}
        return {"stages": stages, "failed_tests": failed, "flaky_tests": flaky,
                "service_processes": procs,
                "evidence_available": {"traces": len(b.spans), "log_lines": len(b.logs),
                                       "changes": bool(b.changes), "deploys": len(b.deploys)}}

    def get_test_failure(self, test_id: str) -> dict:
        t = next((t for t in self.bundle.tests if t.id == test_id), None)
        if t is None:
            return {"error": f"no test {test_id}"}
        return {"test": t.id, "outcome": t.outcome, "passed_on_retry": t.passed_on_retry,
                "message": t.message[:500], "traceback_tail": t.details[-1500:],
                "evidence": self.ledger.cite(f"{t.id}: {t.message[:150]}", t.source)}

    def trace_errors(self, service: str | None = None) -> dict:
        out = []
        for f in analysis.trace_findings(self.bundle):
            paths = [list(p) for p in f.paths]
            if service and f.service != service and not any(service in p for p in paths):
                continue
            path_txt = " -> ".join(paths[0]) if paths else f.service
            out.append({
                "failing_service": f.service, "what_failed": f.kind, "detail": f.detail,
                "operation": f.operation, "requests_affected": f.count, "call_chains": paths,
                "example_traces": f.traces[:2], **({"timing": f.extra} if f.kind == "timeout" else {}),
                "evidence": self.ledger.cite(
                    f"{f.count} failed request(s): {path_txt}; {f.service} {f.kind}: {f.detail}",
                    f.sources[0] if f.sources else "traces/"),
            })
        return {"failure_chains": out,
                "note": "the last service in each chain is where the failure started"}

    def get_trace(self, trace_id: str) -> dict:
        spans = self.bundle.traces().get(trace_id)
        if not spans:
            return {"error": f"no trace {trace_id}"}
        kids: dict[str | None, list] = {}
        ids = {s.span_id for s in spans}
        for s in sorted(spans, key=lambda s: s.start_ns):
            kids.setdefault(s.parent_span_id if s.parent_span_id in ids else None, []).append(s)
        lines: list[str] = []

        def walk(parent, depth):
            for s in kids.get(parent, []):
                if s.kind == "INTERNAL":
                    walk(s.span_id, depth)
                    continue
                exc = s.exception
                status = s.http_status or s.status
                tail = f"  !! {exc['type']}: {exc['message']}" if exc else ""
                lines.append(f"{'  ' * depth}{s.service} {s.kind} {s.name} "
                             f"[{status}, {s.duration_ms:.0f} ms]{tail}")
                walk(s.span_id, depth + 1)

        walk(None, 0)
        first = min(spans, key=lambda s: s.start_ns)
        return {"trace_id": trace_id, "tree": lines[:60],
                "evidence": self.ledger.cite(f"trace {trace_id[:12]}: " + " | ".join(lines[:3]),
                                             first.source)}

    def service_dependencies(self, service: str) -> dict:
        reg = self.bundle.registry
        return {"service": service, "calls": sorted(reg[service].depends_on),
                "called_by": sorted(reg.callers_of(service, transitive=False)),
                "owner": reg[service].owner}

    _MASK = [(re.compile(r"\b[0-9a-f]{8,}\b"), "<hex>"), (re.compile(r"\b(ord|ch|res)_\w+"), "<id>"),
             (re.compile(r"\d+"), "<n>")]

    def log_patterns(self, service: str) -> dict:
        lines = [ln for ln in self.bundle.logs
                 if ln.service == service and ln.level in ("WARNING", "ERROR", "CRITICAL")]
        groups: dict[str, list] = {}
        for ln in lines:
            exc = ln.raw.get("exception") or {}
            text = ln.message + (f" | {exc.get('type')}: {exc.get('message')}" if exc else "")
            key = text
            for pattern, repl in self._MASK:
                key = pattern.sub(repl, key)
            groups.setdefault(key, []).append((ln, text))
        patterns = []
        for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:8]:
            ln, text = items[0]
            patterns.append({"pattern": key, "count": len(items), "level": ln.level,
                             "example": text[:300], "trace_id": ln.trace_id,
                             "evidence": self.ledger.cite(f"{service} log x{len(items)}: {text[:150]}",
                                                          ln.source)})
        return {"service": service, "warning_or_error_lines": len(lines), "patterns": patterns}

    def process_status(self) -> dict:
        out = {}
        for f in analysis.startup_findings(self.bundle):
            out[f.service] = {"status": f.extra["status"], "exit_code": f.extra.get("exit_code"),
                              "error": f.detail, "output_tail": f.extra["tail"][-1200:],
                              "evidence": self.ledger.cite(f"{f.service} {f.extra['status']} on "
                                                           f"start-up: {f.detail}", f.sources[0])}
        for name, info in (self.bundle.stack or {}).get("services", {}).items():
            out.setdefault(name, {"status": info.get("status")})
        return {"processes": out} if out else {"processes": {}, "note": "no process records"}

    def recent_changes(self, service: str) -> dict:
        commits = self.bundle.commits_for(service)
        return {"service": service, "commits": [
            {"sha": c["sha"][:10], "author": c.get("author"), "date": c.get("date"),
             "subject": c["subject"], "files": c.get("files", [])[:10],
             "evidence": self.ledger.cite(f"commit {c['sha'][:7]} \"{c['subject']}\" touched "
                                          f"{service}", "changes.json")}
            for c in commits[:10]],
            "note": "no changes to this service since the last good build" if not commits else ""}

    def build_log(self, stage: str) -> dict:
        st = next((s for s in self.bundle.stages if s.name == stage), None)
        if st is None or not st.log:
            return {"error": f"no log for stage {stage}"}
        text = self.bundle.read_log(st.log)
        lines = text.splitlines()
        hits = [i for i, ln in enumerate(lines) if re.search(r"error|conflict|failed", ln, re.I)]
        start = max(0, (hits[0] if hits else len(lines)) - 5)
        excerpt = lines[start:start + 40]
        return {"stage": stage, "service": st.service, "excerpt": excerpt,
                "evidence": self.ledger.cite(f"build log {stage}: "
                                             f"{(lines[hits[0]] if hits else lines[-1]).strip()[:150]}",
                                             f"{st.log}:{start + 1}")}

    def deploy_history(self) -> dict:
        out = []
        for d in self.bundle.deploys[-10:]:
            out.append({**{k: d.get(k) for k in ("service", "version", "previous_version",
                                                  "outcome", "target")},
                        "reasons": d.get("reasons", [])[-4:],
                        "evidence": self.ledger.cite(
                            f"deploy {d.get('service')} {d.get('version')}: {d.get('outcome')}; "
                            + "; ".join(d.get("reasons", [])[-2:]), "deploys.json")})
        return {"deploys": out}

    def rank_root_causes(self) -> dict:
        ranked = []

        def label(f) -> str:
            return f"{f.service} {f.kind}: {f.detail}" + (f" (x{f.count})" if f.count > 1 else "")

        for h in analysis.hypotheses(self.bundle)[:4]:
            ranked.append({"service": h.service, "category": h.category, "summary": h.summary,
                           "confidence": round(h.confidence, 2), "impact_path": h.impact_path,
                           "suspect_commits": [c["sha"][:10] for c in h.suspect_commits],
                           "evidence": [self.ledger.cite(label(f), f.sources[0] if f.sources
                                                         else "evidence")
                                        for f in h.findings[:3]]})
        return {"hypotheses": ranked}


def dumps(result: Any) -> str:
    return json.dumps(result, default=str, ensure_ascii=False)
