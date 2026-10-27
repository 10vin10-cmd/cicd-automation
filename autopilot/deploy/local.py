"""Deploy to the local stack: the target the demo pipeline really exercises.

A "version" here is the configuration a service starts with; a bad build is
reproduced with a fault or a missing setting. The mechanics are the same as
on a real platform:

1. measure the current version under synthetic checkout traffic (baseline),
2. replace the service process with the candidate and wait for health,
3. measure again (canary) and compare,
4. keep the candidate, or put the previous version back.

Spans and logs from the canary go to the same evidence directory, so a
failed deployment can be diagnosed exactly like a failed test run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from autopilot import stack
from autopilot.deploy.base import DeployResult, Outcome, record
from autopilot.deploy.canary import Sample, Thresholds, Verdict, analyse, p95
from autopilot.registry import Registry


@dataclass
class Candidate:
    version: str
    fault: str | None = None                     # reproduces a bad build
    withhold: list[str] = field(default_factory=list)  # reproduces missing config


def checkout_traffic(registry: Registry, requests: int = 30) -> Sample:
    """Synthetic customer traffic through the front door (orders)."""
    url = f"{registry['orders'].url}/orders"
    errors, latencies = 0, []
    with httpx.Client(timeout=5.0) as client:
        for _ in range(requests):
            start = time.perf_counter()
            try:
                r = client.post(url, json={"sku": "SKU-1001", "qty": 1,
                                           "unit_price_cents": 1999})
                if r.status_code >= 500:
                    errors += 1
            except httpx.HTTPError:
                errors += 1
            latencies.append((time.perf_counter() - start) * 1000)
    return Sample(requests=requests, errors=errors, p95_ms=p95(latencies))


class LocalDeployer:
    name = "local"

    def __init__(self, registry: Registry, evidence: Path, traffic=checkout_traffic,
                 limits: Thresholds = Thresholds(), startup_timeout_s: float = 10.0):
        self.registry = registry
        self.evidence = evidence
        self.traffic = traffic
        self.limits = limits
        self.startup_timeout_s = startup_timeout_s

    def deploy(self, service: str, candidate: Candidate) -> DeployResult:
        state = stack.load_state(self.evidence)
        current = state["services"][service]
        result = DeployResult(service=service, version=candidate.version,
                              previous_version=current.get("version", "current"),
                              outcome=Outcome.PROMOTED, target=self.name)

        baseline = self.traffic(self.registry)
        result.reasons.append(f"baseline: {baseline.requests} requests, "
                              f"{baseline.error_rate:.1%} errors, p95 {baseline.p95_ms:.0f} ms")

        new_pid, health = self._replace(service, candidate)
        if health != "healthy":
            verdict = Verdict(False, [f"candidate {health} on start-up"])
        else:
            canary = self.traffic(self.registry)
            verdict = analyse(canary, baseline, self.limits)
            result.reasons.append(f"canary: {canary.requests} requests, "
                                  f"{canary.error_rate:.1%} errors, p95 {canary.p95_ms:.0f} ms")

        if verdict.promote:
            current.update(pid=new_pid, status="healthy", version=candidate.version)
            result.reasons.append("canary within limits: promoted")
        else:
            result.reasons.extend(verdict.reasons)
            old_pid, old_health = self._replace(service, Candidate(version=result.previous_version))
            current.update(pid=old_pid, status=old_health,
                           version=result.previous_version)
            if old_health == "healthy":
                result.outcome = Outcome.ROLLED_BACK
                result.reasons.append(f"rolled back to {result.previous_version}")
            else:
                result.outcome = Outcome.FAILED
                result.reasons.append(f"rollback failed: previous version {old_health}")

        stack.save_state(self.evidence, state)
        record(self.evidence / "deploys.json", result)
        return result

    def _replace(self, service: str, candidate: Candidate) -> tuple[int, str]:
        state = stack.load_state(self.evidence)
        stack.stop_service(state["services"][service]["pid"])
        opts = stack.StackOptions(
            evidence=self.evidence,
            faults={service: candidate.fault} if candidate.fault else {},
            withhold={service: candidate.withhold} if candidate.withhold else {},
        )
        proc = stack.start_service(service, self.registry, opts, mode="a")
        health = stack.wait_healthy({service: proc}, self.registry, self.startup_timeout_s)
        status = health[service]["status"]
        state["services"][service].update(pid=proc.pid, status=status)
        stack.save_state(self.evidence, state)
        return proc.pid, status
