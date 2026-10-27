"""Deterministic root-cause analysis over an evidence bundle.

The central idea: in a failed request, the service that *returns* the error is
rarely the one that *caused* it. Each failed trace is walked down its span
tree to the deepest failing span, the one with no failing children. That span
says where the failure started and what kind it was:

    server span with an exception   -> code failed in that service
    client span, connection refused -> the called service is down
    client span, read timeout       -> the called service is too slow
    client span, 5xx, no server span-> the called service failed, untraced

Every service above the deepest failing span in the chain is a symptom.
Start-up crashes, flaky tests, build errors and unit-test failures are
detected from their own evidence. Findings are then grouped, matched with
recent commits to the guilty service, and ranked.

This module never calls an LLM. The agents use it as one tool among several,
and the offline agent relies on it entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from autopilot.doctor.evidence import Bundle, Span

CATEGORIES = (
    "code_regression",        # a change made a service throw or misbehave
    "config_error",           # missing or invalid configuration; often fails at start-up
    "dependency_unavailable", # a dependency was down or refused connections
    "latency",                # a dependency answered too slowly for its callers
    "build_failure",          # the build broke: dependencies, syntax, imports
    "test_regression",        # a service's own unit tests fail
    "flaky_test",             # a test failed, then passed on retry with no change
    "unknown",
)


@dataclass
class Finding:
    """One observed failure pattern, before ranking."""
    service: str
    kind: str                 # exception | unreachable | timeout | http_error | startup_crash ...
    detail: str
    operation: str = ""
    count: int = 1
    paths: set[tuple[str, ...]] = field(default_factory=set)   # caller chains, entry first
    sources: list[str] = field(default_factory=list)           # file:line evidence
    traces: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class Hypothesis:
    service: str
    category: str
    summary: str
    confidence: float
    findings: list[Finding] = field(default_factory=list)
    suspect_commits: list[dict] = field(default_factory=list)
    impact_path: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


# -- runtime failures from traces --------------------------------------------

def _children(spans: list[Span]) -> dict[str, list[Span]]:
    kids: dict[str, list[Span]] = {}
    for s in spans:
        if s.parent_span_id:
            kids.setdefault(s.parent_span_id, []).append(s)
    return kids


def _has_error_below(span: Span, kids: dict[str, list[Span]]) -> bool:
    """Is there a failing call or request further down? Framework-internal
    spans (ASGI send/receive) are walked through but never count."""
    stack = list(kids.get(span.span_id, []))
    while stack:
        s = stack.pop()
        if s.kind in ("SERVER", "CLIENT") and s.is_error:
            return True
        stack.extend(kids.get(s.span_id, []))
    return False


def _chain(span: Span, by_id: dict[str, Span]) -> list[str]:
    """Services from the entry point down to this span, without repeats."""
    services: list[str] = []
    cur: Span | None = span
    while cur is not None:
        if not services or services[0] != cur.service:
            services.insert(0, cur.service)
        cur = by_id.get(cur.parent_span_id) if cur.parent_span_id else None
    return services


def trace_findings(bundle: Bundle) -> list[Finding]:
    grouped: dict[tuple, Finding] = {}
    for trace_id, spans in bundle.traces().items():
        if not any(s.is_error for s in spans):
            continue
        by_id = {s.span_id: s for s in spans}
        kids = _children(spans)
        for span in spans:
            if not span.is_error or span.kind not in ("SERVER", "CLIENT"):
                continue
            if _has_error_below(span, kids):
                continue  # not the deepest failure
            finding = _classify(span, by_id, kids, bundle)
            if finding is None:
                continue
            # Timings vary by a millisecond between requests; group timeouts by
            # call, not by their exact numbers.
            detail_key = "" if finding.kind == "timeout" else finding.detail
            key = (finding.service, finding.kind, detail_key, finding.operation)
            agg = grouped.setdefault(key, finding)
            if agg is not finding:
                agg.count += 1
                agg.paths |= finding.paths
            if len(agg.sources) < 3:
                agg.sources.append(span.source)
            if len(agg.traces) < 5:
                agg.traces.append(trace_id)
    return sorted(grouped.values(), key=lambda f: -f.count)


def _classify(span: Span, by_id, kids, bundle: Bundle) -> Finding | None:
    chain = _chain(span, by_id)
    exc = span.exception
    if span.kind == "SERVER":
        if exc:
            return Finding(service=span.service, kind="exception",
                           detail=f"{exc['type']}: {exc['message']}", operation=span.name,
                           paths={tuple(chain)})
        return Finding(service=span.service, kind="http_error",
                       detail=f"HTTP {span.http_status}", operation=span.name,
                       paths={tuple(chain)})

    # CLIENT span: the problem is on the other end of the call.
    peer = bundle.peer_service(span) or "unknown"
    path = tuple(chain + [peer]) if chain[-1] != peer else tuple(chain)
    if exc:
        etype = exc["type"].rsplit(".", 1)[-1]
        if etype in ("ConnectError", "ConnectTimeout") or "refused" in exc["message"].lower():
            return Finding(service=peer, kind="unreachable", detail=f"{etype}: {exc['message']}",
                           operation=span.url or span.name, paths={path},
                           extra={"caller": span.service})
        if "Timeout" in etype:
            server = next((k for k in kids.get(span.span_id, []) if k.kind == "SERVER"), None)
            server = server or _server_span_for(span, bundle)
            extra = {"caller": span.service, "caller_waited_ms": round(span.duration_ms)}
            detail = f"{span.service} gave up after {span.duration_ms:.0f} ms"
            if server:
                extra["callee_took_ms"] = round(server.duration_ms)
                detail = (f"{peer} {server.name} took {server.duration_ms:.0f} ms; "
                          f"{span.service} gave up after {span.duration_ms:.0f} ms")
            return Finding(service=peer, kind="timeout", detail=detail,
                           operation=span.url or span.name, paths={path}, extra=extra)
        return Finding(service=peer, kind="transport_error", detail=f"{etype}: {exc['message']}",
                       operation=span.url or span.name, paths={path})
    return Finding(service=peer, kind="http_error", detail=f"HTTP {span.http_status}",
                   operation=span.url or span.name, paths={path},
                   extra={"caller": span.service, "untraced": True})


def _server_span_for(client: Span, bundle: Bundle) -> Span | None:
    """The callee's server span for a call, matched by trace and parent."""
    for s in bundle.spans:
        if s.trace_id == client.trace_id and s.kind == "SERVER" and \
                s.parent_span_id == client.span_id:
            return s
    return None


# -- start-up crashes ----------------------------------------------------------

_EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(Error|Exception|Exit)): ?(?P<msg>.*)$")


def startup_findings(bundle: Bundle) -> list[Finding]:
    out = []
    services = (bundle.stack or {}).get("services", {})
    for name, info in services.items():
        if info.get("status") not in ("crashed", "unhealthy"):
            continue
        output = bundle.process_output.get(name, "")
        lines = output.strip().splitlines()
        exc_line = next((ln for ln in reversed(lines) if _EXC_LINE.match(ln.strip())), None)
        detail = exc_line.strip() if exc_line else f"process {info.get('status')}"
        line_no = lines.index(exc_line) + 1 if exc_line else len(lines)
        out.append(Finding(service=name, kind="startup_crash", detail=detail,
                           sources=[f"process/{name}.log:{line_no}", "stack.json"],
                           extra={"status": info.get("status"), "exit_code": info.get("exit_code"),
                                  "tail": "\n".join(lines[-8:])}))
    return out


# -- tests and builds ----------------------------------------------------------

def flaky_findings(bundle: Bundle) -> list[Finding]:
    return [Finding(service=_service_of_test(t.id, bundle) or "tests", kind="flaky_test",
                    detail=f"{t.id} failed ({t.message[:120]}) then passed on retry",
                    sources=[t.source], extra={"test": t.id, "message": t.message})
            for t in bundle.flaky_tests()]


def _service_of_test(test_id: str, bundle: Bundle) -> str | None:
    return bundle.registry.service_for_path(test_id.split("::")[0])


_BUILD_PATTERNS = [
    ("dependency_not_found",
     re.compile(r"No matching distribution found for (?P<what>\S+)")),
    ("dependency_conflict",
     re.compile(r"Cannot install (?P<what>.+?) because these package versions have conflicting "
                r"dependencies")),
    ("dependency_conflict", re.compile(r"(ResolutionImpossible|conflicting dependencies)")),
    ("syntax_error", re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)'
                                r"[\s\S]*?SyntaxError: (?P<what>.+)")),
    ("missing_module", re.compile(r"ModuleNotFoundError: No module named '(?P<what>[^']+)'")),
]


def build_findings(bundle: Bundle) -> list[Finding]:
    out = []
    for stage in bundle.failed_stages():
        if stage.kind != "build":
            continue
        text = bundle.read_log(stage.log) if stage.log else stage.summary
        lines = text.splitlines()
        kind, detail, line_no = "build_error", "", 0
        for name, pattern in _BUILD_PATTERNS:
            m = pattern.search(text)
            if m:
                kind = name
                detail = m.group(0).strip().splitlines()[-1]
                line_no = text[: m.start()].count("\n") + 1
                break
        if not detail:
            errs = [ln for ln in lines if "error" in ln.lower()]
            detail = (errs[-1] if errs else (lines[-1] if lines else "build failed")).strip()
        conflict = _conflict_lines(lines) if kind == "dependency_conflict" else []
        out.append(Finding(service=stage.service or "unknown", kind=kind, detail=detail,
                           sources=[f"{stage.log}:{line_no}" if stage.log else "stages.json"],
                           extra={"stage": stage.name, "conflict": conflict}))
    return out


def _conflict_lines(lines: list[str]) -> list[str]:
    """The lines pip prints under "The conflict is caused by:", up to the
    next blank line."""
    for i, ln in enumerate(lines):
        if "The conflict is caused by" in ln:
            out = []
            for x in lines[i + 1:i + 8]:
                if not x.strip():
                    break
                out.append(x.strip())
            return out
    return []


def unit_findings(bundle: Bundle) -> list[Finding]:
    out = []
    failed_units = {s.service for s in bundle.failed_stages() if s.kind == "unit"}
    for t in bundle.failed_tests():
        svc = _service_of_test(t.id, bundle)
        if t.passed_on_retry or svc is None or svc not in failed_units:
            continue
        out.append(Finding(service=svc, kind="unit_failure", detail=f"{t.id}: {t.message[:200]}",
                           sources=[t.source], extra={"test": t.id}))
    return out


# -- ranking -------------------------------------------------------------------

def hypotheses(bundle: Bundle) -> list[Hypothesis]:
    flaky = flaky_findings(bundle)
    hard_failure = bool(bundle.failed_stages()) or any(
        not t.passed_on_retry for t in bundle.failed_tests())
    if flaky and not hard_failure:
        return [_flaky_only(bundle, flaky)]
    findings = (build_findings(bundle) + unit_findings(bundle) + startup_findings(bundle)
                + trace_findings(bundle) + flaky)
    crashed = {f.service: f for f in findings if f.kind == "startup_crash"}
    by_service: dict[str, list[Finding]] = {}
    for f in findings:
        by_service.setdefault(f.service, []).append(f)

    out: list[Hypothesis] = []
    for service, fs in by_service.items():
        kinds = {f.kind for f in fs}
        commits = bundle.commits_for(service)
        h = Hypothesis(service=service, category="unknown", summary="", confidence=0.3,
                       findings=fs, suspect_commits=commits[:3])

        if kinds & {"dependency_not_found", "dependency_conflict", "syntax_error",
                    "missing_module", "build_error"}:
            f = fs[0]
            h.category, h.confidence = "build_failure", 0.9
            h.summary = f"{service} failed to build: {f.detail}"
        elif "startup_crash" in kinds:
            f = crashed[service]
            config = bool(re.search(r"KeyError|environ|config|Setting|\.env", f.detail))
            h.category = "config_error" if config else "code_regression"
            h.confidence = 0.92 if config else 0.8
            h.summary = f"{service} crashed on start-up ({f.detail})"
            if "unreachable" in kinds:
                callers = sorted({x.extra.get("caller") for x in fs if x.kind == "unreachable"} - {None})
                h.summary += f", so calls from {', '.join(callers)} were refused"
        elif "exception" in kinds:
            f = next(x for x in fs if x.kind == "exception")
            h.category = "code_regression"
            h.confidence = 0.88 if commits else 0.7
            h.summary = f"{service} raises {f.detail} in {f.operation}"
        elif "timeout" in kinds:
            f = next(x for x in fs if x.kind == "timeout")
            h.category, h.confidence = "latency", 0.8 if commits else 0.7
            h.summary = f"{service} is too slow for its callers: {f.detail}"
        elif "unreachable" in kinds:
            h.category, h.confidence = "dependency_unavailable", 0.65
            h.summary = f"{service} refused connections"
        elif "unit_failure" in kinds:
            h.category, h.confidence = "test_regression", 0.85 if commits else 0.7
            failed = [f.extra["test"] for f in fs if f.kind == "unit_failure"]
            h.summary = f"{len(failed)} unit test(s) failing in {service}"
        elif kinds == {"flaky_test"}:
            h.category, h.confidence = "flaky_test", 0.6
            h.summary = f"flaky test: {fs[0].extra['test']} failed then passed on retry"
        elif "http_error" in kinds:
            h.category, h.confidence = "code_regression", 0.55
            h.summary = f"{service} returned server errors"

        if commits and h.category in ("code_regression", "latency", "config_error",
                                      "test_regression", "build_failure"):
            h.summary += f"; changed in {commits[0]['sha'][:7]} \"{commits[0]['subject']}\""
        h.impact_path = _impact_path(fs)
        h.actions = _actions(h)
        out.append(h)

    # A flake on its own is real but minor next to a hard failure elsewhere.
    hard = [h for h in out if h.category != "flaky_test"]
    if hard:
        for h in out:
            if h.category == "flaky_test":
                h.confidence = min(h.confidence, 0.3)
    return sorted(out, key=lambda h: (-h.confidence, -sum(f.count for f in h.findings)))


def _flaky_only(bundle: Bundle, flaky: list[Finding]) -> Hypothesis:
    """Every failure passed on retry: the errors in the traces belong to the
    flake, not to a regression. Name the service that produced them."""
    errors = trace_findings(bundle)
    service = errors[0].service if errors else "unknown"
    tests = [f.extra["test"] for f in flaky]
    detail = f" ({service} returned {errors[0].detail} once)" if errors else ""
    h = Hypothesis(service=service, category="flaky_test", confidence=0.75,
                   summary=f"flaky: {', '.join(tests)} failed once{detail}, then passed on retry",
                   findings=flaky + errors[:1], impact_path=[service] if errors else [])
    h.actions = _actions(h)
    return h


def symptoms(bundle: Bundle, root_service: str) -> list[dict]:
    """Services that failed only because the root-cause service did."""
    out = []
    for f in trace_findings(bundle):
        for path in f.paths:
            if root_service in path:
                for svc in path[: path.index(root_service)]:
                    if svc not in {o["service"] for o in out}:
                        out.append({"service": svc, "why": f"returned errors because calls to "
                                    f"{root_service} failed ({f.kind})"})
    return out


def _impact_path(findings: list[Finding]) -> list[str]:
    paths = sorted({p for f in findings for p in f.paths}, key=len, reverse=True)
    return list(paths[0]) if paths else ([findings[0].service] if findings else [])


def _actions(h: Hypothesis) -> list[str]:
    c = h.suspect_commits[0] if h.suspect_commits else None
    ref = f"{c['sha'][:7]} (\"{c['subject']}\")" if c else "the most recent change"
    caller = h.impact_path[-2] if len(h.impact_path) > 1 else None
    if h.category == "code_regression":
        acts = [f"Revert or fix {ref} in {h.service}."]
        if caller:
            acts.append(f"Add a contract test between {caller} and {h.service} so an "
                        f"incompatible API change fails in the {h.service} pipeline.")
        return acts
    if h.category == "config_error":
        key = re.search(r"KeyError: '([^']+)'", " ".join(f.detail for f in h.findings))
        what = f"`{key.group(1)}`" if key else "the missing setting"
        return [f"Set {what} for {h.service} in its deployment configuration.",
                "Validate required configuration in the deploy stage, before traffic shifts."]
    if h.category == "latency":
        return [f"Profile {h.service}: its responses now exceed callers' timeouts.",
                f"If {ref} is the cause, roll it back; otherwise review the timeout and retry "
                "budget between the services."]
    if h.category == "dependency_unavailable":
        return [f"Check why {h.service} is not accepting connections (crash, port, health check)."]
    if h.category == "build_failure":
        conflict = next((f.extra.get("conflict") for f in h.findings if f.extra.get("conflict")), None)
        acts = [f"Fix the build of {h.service}: {h.findings[0].detail}"]
        if conflict:
            acts.append("Resolve the conflicting pins: " + "; ".join(conflict[:3]))
        return acts
    if h.category == "test_regression":
        return [f"Fix the failing tests in {h.service} or revert {ref}."]
    if h.category == "flaky_test":
        test = h.findings[0].extra.get("test", "the test")
        first = " ".join(str(f.extra.get("message", "")) + f.detail for f in h.findings)
        return [f"Quarantine {test} and file a fix; it is passing on retry, not reliably.",
                f"Look for a start-up race in {h.service}: the first request after start-up "
                "failed. Gate traffic on a readiness check that reflects warm-up."
                if "503" in first or "warming" in first
                else "Look for shared state, timing or ordering assumptions in the test."]
    return ["Inspect the evidence below; the automated analysis could not classify this."]
