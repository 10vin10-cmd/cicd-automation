"""The investigation loop, driven by a scripted stand-in for the LLM."""

import json

from builders import EvidenceBuilder

from autopilot.doctor import report
from autopilot.doctor.agents import (
    LLMInvestigator,
    PlaybookInvestigator,
    diagnose,
    route,
)
from autopilot.doctor.evidence import Bundle
from autopilot.doctor.llm import ToolCall, Turn
from autopilot.doctor.tools import Toolbox


class ScriptedLLM:
    """Plays back a list of moves. A move is a function of the conversation so
    far that returns a Turn, so it can use evidence IDs from earlier results."""
    name = "scripted"
    model = "test"

    def __init__(self, moves):
        self.moves = list(moves)
        self.seen_tools: list[list[str]] = []

    def turn(self, system, messages, tools):
        self.seen_tools.append([t.name for t in tools])
        move = self.moves.pop(0) if self.moves else (lambda m: Turn("thinking..."))
        return move(messages)


def call(name, **args):
    return lambda messages: Turn("", [ToolCall(f"c{len(messages)}", name, args)])


def results(messages) -> list[dict]:
    return [json.loads(b["content"]) for m in messages for b in m["content"]
            if b["type"] == "tool_result"]


def evidence_ids(messages) -> list[str]:
    ids = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "evidence":
                    ids.extend(v if isinstance(v, list) else [v])
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(results(messages))
    return ids


def submit(evidence_fn, **overrides):
    def move(messages):
        args = {"root_cause_service": "payments", "category": "code_regression",
                "summary": "payments raises KeyError on the renamed field",
                "impact_path": ["orders", "payments"], "evidence": evidence_fn(messages),
                "confidence": 0.9, "actions": ["Revert the rename"], **overrides}
        return Turn("", [ToolCall(f"s{len(messages)}", "submit_diagnosis", args)])
    return move


def bundle(tmp_path) -> Bundle:
    b = EvidenceBuilder(tmp_path)
    b.failed_checkout_via_payments_exception()
    b.stage(name="integration-tests", kind="integration", status="failure")
    b.commit("payments", "4f9c2e1ab7d0", "Rename amount_cents to amount")
    return Bundle.load(b.write())


def test_agent_investigates_and_submits_a_cited_diagnosis(tmp_path):
    llm = ScriptedLLM([
        call("pipeline_overview"),
        call("trace_errors", service="payments"),
        call("recent_changes", service="payments"),
        submit(lambda m: evidence_ids(m)[-2:]),
    ])
    d, toolbox = diagnose(bundle(tmp_path), llm)
    assert d.root_cause_service == "payments"
    assert d.agent == "scripted:test"
    assert d.specialist == "runtime"
    assert [s.tool for s in d.steps] == ["pipeline_overview", "trace_errors", "recent_changes",
                                         "submit_diagnosis"]
    assert all(e in toolbox.ledger for e in d.evidence)
    assert d.not_the_cause[0]["service"] == "orders"


def test_invented_evidence_is_rejected_and_the_agent_corrects_itself(tmp_path):
    llm = ScriptedLLM([
        call("trace_errors"),
        submit(lambda m: ["E99"]),                      # made up
        submit(lambda m: evidence_ids(m)[:1]),          # corrected
    ])
    d, _ = diagnose(bundle(tmp_path), llm)
    rejected = [s for s in d.steps if s.tool == "submit_diagnosis" and s.error]
    assert len(rejected) == 1
    assert "E99" in rejected[0].result_summary
    assert d.agent == "scripted:test"


def test_falls_back_to_the_playbook_when_the_model_never_concludes(tmp_path):
    llm = ScriptedLLM([call("pipeline_overview")])  # then only "thinking..."
    d, _ = LLMInvestigator(llm, max_steps=4).investigate(Toolbox(bundle(tmp_path))), None
    assert d.root_cause_service == "payments"
    assert d.agent == "scripted → playbook"
    assert "did not conclude within 4 turns" in d.notes[0]


def test_repeated_invalid_submissions_fall_back(tmp_path):
    llm = ScriptedLLM([submit(lambda m: ["E1000"])] * 4)
    d = LLMInvestigator(llm, max_rejections=2).investigate(Toolbox(bundle(tmp_path)))
    assert "failed validation repeatedly" in d.notes[0]


def test_specialist_only_gets_its_tools(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.file("build/payments.log", "ERROR: No matching distribution found for nope==9\n")
    b.stage(name="build-payments", kind="build", status="failure", service="payments",
            log="build/payments.log")
    bundle_ = Bundle.load(b.write())
    assert route(bundle_) == "build"
    llm = ScriptedLLM([call("trace_errors")])
    d = LLMInvestigator(llm, max_steps=2).investigate(Toolbox(bundle_))
    assert "trace_errors" not in llm.seen_tools[0]
    assert d.steps[0].result_summary == "not allowed"


def test_tool_errors_are_returned_to_the_model_not_raised(tmp_path):
    llm = ScriptedLLM([
        call("get_trace", trace_id="does-not-exist"),
        call("recent_changes", service="nope"),
        submit(lambda m: ["E1"]),
    ])
    tb = Toolbox(bundle(tmp_path))
    tb.call("pipeline_overview", {})  # issues E1
    d = LLMInvestigator(llm).investigate(tb)
    assert d.steps[0].error and d.steps[1].error
    assert "unknown service 'nope'" in d.steps[1].result_summary
    assert d.root_cause_service == "payments"


def test_playbook_and_report(tmp_path):
    d, tb = diagnose(bundle(tmp_path))
    assert d.agent == "playbook"
    md = report.markdown(d, tb.ledger)
    assert "## 🔎 Root cause: `payments` — code regression" in md
    assert "**Impact:** orders → payments" in md
    assert "**Not the cause:** `orders`" in md
    for eid in d.evidence:
        assert f"| {eid} |" in md
    doc = json.loads(report.as_json(d, tb.ledger))
    assert doc["evidence"][0]["source"]


def test_playbook_reports_unknown_when_nothing_explains_the_failure(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.stage(name="integration-tests", kind="integration", status="failure")
    d = PlaybookInvestigator().investigate(Toolbox(Bundle.load(b.write())))
    assert d.root_cause_service == "unknown"
    assert d.confidence < 0.5
