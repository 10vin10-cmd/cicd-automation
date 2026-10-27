"""The investigating agents.

Routing is deterministic: the stage that failed decides which specialist
investigates (build, unit tests, or runtime: integration tests, deploys and
canaries). Each specialist gets a focused brief and only the tools that make
sense for it.

Investigation is agentic when an LLM is configured: the model decides which
tool to call next and when it has enough to conclude. Its conclusion must go
through `submit_diagnosis`, and is rejected (with the reason, so it can
correct itself) unless every cited evidence ID was actually returned by a
tool. If the model doesn't converge within its step budget, the offline
playbook's answer is used instead and the report says so.

Without an LLM, the playbook runs the same tools in a fixed order. Both paths
produce the same Diagnosis and the same investigation transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from autopilot.doctor import analysis
from autopilot.doctor.analysis import CATEGORIES
from autopilot.doctor.evidence import Bundle
from autopilot.doctor.llm import LLM
from autopilot.doctor.tools import SUBMIT, Toolbox, dumps

COMMON = ["pipeline_overview", "rank_root_causes", "recent_changes", SUBMIT]
SPECIALISTS = {
    "build": {
        "brief": "A build stage failed. Read the build log, identify the exact error "
                 "(dependency resolution, syntax, missing module) and the service it belongs to, "
                 "and connect it to the commit that introduced it.",
        "tools": COMMON + ["build_log"],
    },
    "unit": {
        "brief": "Unit tests failed. The failure is almost always in the service whose tests "
                 "failed. Read the failures and connect them to the commits that changed that "
                 "service.",
        "tools": COMMON + ["get_test_failure"],
    },
    "runtime": {
        "brief": "Integration tests, a deployment or a canary failed. The service that "
                 "reported the error is often not the cause. Follow the failing calls through "
                 "the traces to the deepest failing service, check whether it even started, "
                 "and connect it to recent changes or deployments.",
        "tools": COMMON + ["trace_errors", "get_trace", "service_dependencies", "log_patterns",
                           "process_status", "deploy_history", "get_test_failure"],
    },
}

SYSTEM = """You are the root-cause investigator for a CI/CD pipeline. A run failed and
engineers want to know why without reading logs themselves.

How to work:
- Use the tools. Never assume a fact you have not seen in a tool result.
- Separate cause from symptom. A service that returns errors because something it
  calls failed is a symptom. The root cause is the deepest failing component.
- Prefer trace evidence over log evidence; use logs and process output to confirm.
- Check recent changes and deployments for the root-cause service.
- A test that failed then passed on retry is flaky; mention it, but a hard failure
  elsewhere is the root cause.
- Stop investigating once the evidence is conclusive. Then call submit_diagnosis
  once, citing the evidence IDs (E1, E2, ...) that tool results gave you. Do not
  invent IDs.

Specialist focus: {brief}"""


@dataclass
class Step:
    tool: str
    args: dict
    result_summary: str
    error: bool = False


@dataclass
class Diagnosis:
    root_cause_service: str
    category: str
    summary: str
    confidence: float
    evidence: list[str]
    actions: list[str]
    impact_path: list[str] = field(default_factory=list)
    suspect_commits: list[str] = field(default_factory=list)
    not_the_cause: list[dict] = field(default_factory=list)
    agent: str = ""
    specialist: str = ""
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["steps"] = [s.__dict__ for s in self.steps]
        return d


def route(bundle: Bundle) -> str:
    kinds = {s.kind for s in bundle.failed_stages()}
    if "build" in kinds:
        return "build"
    if "unit" in kinds:
        return "unit"
    return "runtime"


def validate_submission(args: dict, toolbox: Toolbox) -> list[str]:
    errors = []
    services = set(toolbox.bundle.registry.services) | {"unknown"}
    if args.get("root_cause_service") not in services:
        errors.append(f"root_cause_service must be one of {sorted(services)}")
    if args.get("category") not in CATEGORIES:
        errors.append(f"category must be one of {list(CATEGORIES)}")
    evidence = args.get("evidence") or []
    if not evidence:
        errors.append("cite at least one evidence ID")
    unknown = [e for e in evidence if e not in toolbox.ledger]
    if unknown:
        errors.append(f"these evidence IDs were never returned by a tool: {unknown}")
    conf = args.get("confidence")
    if not isinstance(conf, int | float) or not 0 <= conf <= 1:
        errors.append("confidence must be a number between 0 and 1")
    if not args.get("actions"):
        errors.append("give at least one concrete action")
    if not str(args.get("summary", "")).strip():
        errors.append("summary is required")
    return errors


def _summarise(result: dict) -> str:
    text = dumps(result)
    return text if len(text) <= 240 else text[:237] + "..."


# -- LLM-driven investigation ---------------------------------------------------

class LLMInvestigator:
    def __init__(self, llm: LLM, max_steps: int = 12, max_rejections: int = 2):
        self.llm, self.max_steps, self.max_rejections = llm, max_steps, max_rejections

    def investigate(self, toolbox: Toolbox) -> Diagnosis:
        specialist = route(toolbox.bundle)
        spec = SPECIALISTS[specialist]
        tools = toolbox.specs(spec["tools"])
        allowed = {t.name for t in tools}
        system = SYSTEM.format(brief=spec["brief"])
        messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text":
            "The pipeline run failed. Find the root cause. Start with pipeline_overview."}]}]
        steps: list[Step] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
        rejections = 0

        for _ in range(self.max_steps):
            turn = self.llm.turn(system, messages, tools)
            usage["turns"] += 1
            for k in ("input_tokens", "output_tokens"):
                usage[k] += turn.usage.get(k, 0)
            content = ([{"type": "text", "text": turn.text}] if turn.text else []) + [
                {"type": "tool_call", "id": c.id, "name": c.name, "input": c.input}
                for c in turn.tool_calls]
            messages.append({"role": "assistant", "content": content or
                             [{"type": "text", "text": "(no output)"}]})
            if not turn.tool_calls:
                messages.append({"role": "user", "content": [{"type": "text", "text":
                    "Continue the investigation with the tools, or call submit_diagnosis."}]})
                continue

            results = []
            for call in turn.tool_calls:
                if call.name == SUBMIT:
                    errors = validate_submission(call.input, toolbox)
                    if not errors:
                        return self._diagnosis(call.input, toolbox, specialist, steps, usage)
                    rejections += 1
                    steps.append(Step(SUBMIT, call.input, "rejected: " + "; ".join(errors), True))
                    results.append(_result(call.id, {"rejected": errors}, True))
                    if rejections > self.max_rejections:
                        return self._fallback(toolbox, specialist, steps, usage,
                                              "the model's diagnosis failed validation repeatedly")
                    continue
                if call.name not in allowed:
                    results.append(_result(call.id, {"error": f"tool {call.name} is not "
                                                     f"available to the {specialist} specialist"}, True))
                    steps.append(Step(call.name, call.input, "not allowed", True))
                    continue
                try:
                    out = toolbox.call(call.name, call.input)
                    results.append(_result(call.id, out, "error" in out))
                    steps.append(Step(call.name, call.input, _summarise(out), "error" in out))
                except Exception as exc:  # a bad argument must not end the run
                    results.append(_result(call.id, {"error": f"{type(exc).__name__}: {exc}"}, True))
                    steps.append(Step(call.name, call.input, f"error: {exc}", True))
            messages.append({"role": "user", "content": results})

        return self._fallback(toolbox, specialist, steps, usage,
                              f"the model did not conclude within {self.max_steps} turns")

    def _diagnosis(self, args, toolbox, specialist, steps, usage) -> Diagnosis:
        root = args["root_cause_service"]
        return Diagnosis(
            root_cause_service=root, category=args["category"], summary=args["summary"],
            confidence=float(args["confidence"]), evidence=list(args["evidence"]),
            actions=list(args["actions"]), impact_path=list(args.get("impact_path") or []),
            suspect_commits=list(args.get("suspect_commits") or []),
            not_the_cause=analysis.symptoms(toolbox.bundle, root) if root != "unknown" else [],
            agent=f"{self.llm.name}:{getattr(self.llm, 'model', '')}", specialist=specialist,
            steps=steps + [Step(SUBMIT, {"root_cause_service": root}, "accepted")], usage=usage,
        )

    def _fallback(self, toolbox, specialist, steps, usage, why) -> Diagnosis:
        d = PlaybookInvestigator().investigate(toolbox)
        d.notes.append(f"Fell back to the offline playbook: {why}.")
        d.agent = f"{self.llm.name} → playbook"
        d.steps = steps + d.steps
        d.usage = usage
        return d


def _result(call_id: str, payload: dict, is_error: bool) -> dict:
    return {"type": "tool_result", "id": call_id, "content": json.dumps(payload, default=str),
            "is_error": is_error}


# -- offline playbook --------------------------------------------------------------

class PlaybookInvestigator:
    """Runs the investigation tools in a fixed order and concludes from the
    deterministic analysis, verified against trace, process, log and change
    evidence. No network access, no model, same output shape."""
    name = "playbook"

    def investigate(self, toolbox: Toolbox) -> Diagnosis:
        steps: list[Step] = []
        cited: list[str] = []

        def call(tool: str, **args) -> dict:
            out = toolbox.call(tool, args)
            steps.append(Step(tool, args, _summarise(out), "error" in out))
            return out

        specialist = route(toolbox.bundle)
        overview = call("pipeline_overview")
        ranked = call("rank_root_causes")["hypotheses"]
        hyps = analysis.hypotheses(toolbox.bundle)
        if not hyps:
            failing = [s["evidence"] for s in overview["stages"] if s["status"] == "failure"]
            failing += [t["evidence"] for t in overview["failed_tests"]]
            return Diagnosis(
                root_cause_service="unknown", category="unknown",
                summary="The run failed, but the evidence shows no failing request, crash, "
                        "build error or flaky test to explain it.",
                confidence=0.1, evidence=failing or ["E1"],
                actions=["Check that traces and logs were collected for this run."],
                agent=self.name, specialist=specialist, steps=steps)

        top, top_ranked = hyps[0], ranked[0]
        cited += top_ranked["evidence"]
        svc = top.service

        if top.category == "build_failure":
            stage = next((f.extra.get("stage") for f in top.findings if f.extra.get("stage")), None)
            if stage:
                cited.append(call("build_log", stage=stage)["evidence"])
        elif top.category == "flaky_test":
            test = top.findings[0].extra.get("test")
            if test:
                cited.append(call("get_test_failure", test_id=test)["evidence"])
        elif top.category == "test_regression":
            test = top.findings[0].extra.get("test")
            if test:
                cited.append(call("get_test_failure", test_id=test)["evidence"])
        else:
            chains = call("trace_errors", service=svc)["failure_chains"]
            cited += [c["evidence"] for c in chains if c["failing_service"] == svc][:2]
            if top.category in ("config_error", "dependency_unavailable"):
                proc = call("process_status")["processes"].get(svc, {})
                if "evidence" in proc:
                    cited.append(proc["evidence"])
            logs = call("log_patterns", service=svc)["patterns"]
            cited += [p["evidence"] for p in logs[:1]]
            if toolbox.bundle.deploys:
                deploys = call("deploy_history")["deploys"]
                cited += [d["evidence"] for d in deploys if d["service"] == svc][-1:]

        changes = call("recent_changes", service=svc)["commits"]
        cited += [c["evidence"] for c in changes[:1]]

        notes = []
        others = [h for h in hyps[1:] if h.confidence >= 0.5]
        if others:
            notes.append("Also seen: " + "; ".join(f"{h.service}: {h.summary}" for h in others[:2]))
        flaky = [t["test"] for t in overview["flaky_tests"]]
        if flaky and top.category != "flaky_test":
            notes.append(f"Flaky (passed on retry): {', '.join(flaky)}")

        args = {"root_cause_service": svc, "category": top.category, "summary": top.summary,
                "evidence": list(dict.fromkeys(cited)), "confidence": round(top.confidence, 2),
                "actions": top.actions}
        errors = validate_submission(args, toolbox)
        if errors:  # would be a bug in the playbook itself
            raise RuntimeError(f"playbook produced an invalid diagnosis: {errors}")
        steps.append(Step(SUBMIT, {"root_cause_service": svc}, "accepted"))
        return Diagnosis(
            root_cause_service=svc, category=top.category, summary=top.summary,
            confidence=round(top.confidence, 2), evidence=args["evidence"], actions=top.actions,
            impact_path=top.impact_path,
            suspect_commits=[c["sha"][:10] for c in top.suspect_commits],
            not_the_cause=analysis.symptoms(toolbox.bundle, svc),
            agent=self.name, specialist=specialist, steps=steps, notes=notes)


def diagnose(bundle: Bundle, llm: LLM | None = None) -> tuple[Diagnosis, Toolbox]:
    toolbox = Toolbox(bundle)
    investigator = LLMInvestigator(llm) if llm else PlaybookInvestigator()
    return investigator.investigate(toolbox), toolbox
