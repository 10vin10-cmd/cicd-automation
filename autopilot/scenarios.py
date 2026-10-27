"""Fault scenarios: reproduce a failure, run the pipeline stages against it,
diagnose, and check the diagnosis against the known answer.

This is the evaluation suite for the agents. CI runs it with the offline
playbook on every change; run it with an LLM backend configured to measure a
model the same way:

    autopilot scenario eval                        # offline playbook
    AUTOPILOT_LLM=bedrock AUTOPILOT_LLM_MODEL=... autopilot scenario eval

Modes:
  live      start the services with the fault, run integration tests
  deploy    start healthy services, deploy a faulty candidate with canary
  recorded  replay recorded stage output (e.g. a real failed build log)
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from autopilot import stack
from autopilot.deploy.local import Candidate, LocalDeployer, checkout_traffic
from autopilot.doctor import report
from autopilot.doctor.agents import Diagnosis, diagnose
from autopilot.doctor.evidence import Bundle
from autopilot.doctor.llm import LLM
from autopilot.registry import ROOT, Registry
from autopilot.testing import run_suite

SCENARIOS = ROOT / "scenarios"


@dataclass
class Scenario:
    name: str
    description: str
    mode: str
    expect: dict
    raw: dict

    @classmethod
    def load(cls, name: str) -> Scenario:
        raw = yaml.safe_load((SCENARIOS / f"{name}.yaml").read_text())
        return cls(name=name, description=" ".join(raw["description"].split()),
                   mode=raw["mode"], expect=raw["expect"], raw=raw)


def names() -> list[str]:
    return sorted(p.stem for p in SCENARIOS.glob("*.yaml"))


@dataclass
class Outcome:
    scenario: Scenario
    diagnosis: Diagnosis
    markdown: str
    evidence_dir: Path

    @property
    def correct(self) -> bool:
        return (self.diagnosis.root_cause_service == self.scenario.expect["root_cause_service"]
                and self.diagnosis.category == self.scenario.expect["category"])


def reproduce(sc: Scenario, evidence: Path, registry: Registry) -> None:
    """Create the evidence a real pipeline run would have left behind."""
    if evidence.exists():
        shutil.rmtree(evidence)
    evidence.mkdir(parents=True)
    stages: list[dict] = []

    if sc.mode == "recorded":
        shutil.copytree(SCENARIOS / "recorded" / sc.name, evidence, dirs_exist_ok=True)
        stages = sc.raw["stages"]

    elif sc.mode == "live":
        opts = stack.StackOptions(evidence=evidence, faults=sc.raw.get("faults", {}),
                                  withhold=sc.raw.get("withhold", {}), startup_timeout_s=10)
        try:
            stack.up(registry, opts)
            result = run_suite("integration", [str(ROOT / "tests" / "integration")], evidence)
        finally:
            stack.down(evidence)
        stages.append({"name": "integration-tests", "kind": "integration",
                       "status": "success" if result.passed else "failure",
                       "summary": f"failed: {result.failed_after_retry}; flaky: {result.flaky}"})

    elif sc.mode == "deploy":
        spec = sc.raw["deploy"]
        try:
            stack.up(registry, stack.StackOptions(evidence=evidence))
            result = run_suite("integration", [str(ROOT / "tests" / "integration")], evidence)
            stages.append({"name": "integration-tests", "kind": "integration",
                           "status": "success" if result.passed else "failure"})
            deployer = LocalDeployer(registry, evidence,
                                     traffic=lambda reg: checkout_traffic(reg, requests=20))
            outcome = deployer.deploy(spec["service"], Candidate(
                version=spec["version"], fault=spec.get("fault"),
                withhold=spec.get("withhold", [])))
        finally:
            stack.down(evidence)
        stages.append({"name": f"deploy-staging-{spec['service']}", "kind": "deploy",
                       "status": "success" if outcome.ok else "failure",
                       "service": spec["service"], "summary": "; ".join(outcome.reasons)})
    else:
        raise ValueError(f"unknown scenario mode {sc.mode}")

    (evidence / "stages.json").write_text(json.dumps(stages, indent=2))
    (evidence / "changes.json").write_text(json.dumps({"commits": sc.raw.get("changes") or {}},
                                                      indent=2))


def run(name: str, evidence_root: Path, llm: LLM | None = None,
        registry: Registry | None = None) -> Outcome:
    registry = registry or Registry.load()
    sc = Scenario.load(name)
    evidence = evidence_root / name
    reproduce(sc, evidence, registry)
    diagnosis, toolbox = diagnose(Bundle.load(evidence, registry), llm)
    md = report.markdown(diagnosis, toolbox.ledger)
    (evidence / "diagnosis.md").write_text(md)
    (evidence / "diagnosis.json").write_text(report.as_json(diagnosis, toolbox.ledger))
    return Outcome(sc, diagnosis, md, evidence)


def scoreboard(outcomes: list[Outcome]) -> str:
    rows = ["| Scenario | Expected | Diagnosed | Confidence | Tool calls | |",
            "|---|---|---|---|---|---|"]
    for o in outcomes:
        exp = f"{o.scenario.expect['root_cause_service']} / {o.scenario.expect['category']}"
        got = f"{o.diagnosis.root_cause_service} / {o.diagnosis.category}"
        calls = sum(1 for s in o.diagnosis.steps if s.tool != "submit_diagnosis")
        rows.append(f"| {o.scenario.name} | {exp} | {got} | {o.diagnosis.confidence:.0%} | "
                    f"{calls} | {'✅' if o.correct else '❌'} |")
    correct = sum(o.correct for o in outcomes)
    rows.append("")
    rows.append(f"**{correct}/{len(outcomes)} correct** "
                f"(agent: {outcomes[0].diagnosis.agent if outcomes else '-'})")
    return "\n".join(rows)
