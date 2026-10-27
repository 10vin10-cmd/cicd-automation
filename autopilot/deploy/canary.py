"""Canary analysis: is the new version healthy enough to keep?

Compares the canary's error rate and latency with absolute limits and, when a
baseline is available, with the version it replaces. Pure functions, so the
promote/rollback rule is easy to test and to explain in a post-incident review.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Sample:
    requests: int
    errors: int          # 5xx responses or transport failures
    p95_ms: float

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 1.0


@dataclass(frozen=True)
class Thresholds:
    max_error_rate: float = 0.02         # 2% absolute
    max_error_rate_increase: float = 0.01  # +1 point over baseline
    max_p95_ms: float = 800
    max_p95_regression: float = 1.5      # 50% slower than baseline
    min_requests: int = 20               # too little traffic proves nothing


@dataclass
class Verdict:
    promote: bool
    reasons: list[str] = field(default_factory=list)


def analyse(canary: Sample, baseline: Sample | None = None,
            limits: Thresholds = Thresholds()) -> Verdict:
    reasons = []
    if canary.requests < limits.min_requests:
        reasons.append(f"only {canary.requests} requests; need {limits.min_requests} to judge")
    if canary.error_rate > limits.max_error_rate:
        reasons.append(f"error rate {canary.error_rate:.1%} > {limits.max_error_rate:.0%}")
    if canary.p95_ms > limits.max_p95_ms:
        reasons.append(f"p95 {canary.p95_ms:.0f} ms > {limits.max_p95_ms:.0f} ms")
    if baseline and baseline.requests >= limits.min_requests:
        if canary.error_rate - baseline.error_rate > limits.max_error_rate_increase:
            reasons.append(f"error rate up {canary.error_rate - baseline.error_rate:.1%} "
                           "over the current version")
        if baseline.p95_ms > 0 and canary.p95_ms > baseline.p95_ms * limits.max_p95_regression:
            reasons.append(f"p95 {canary.p95_ms:.0f} ms vs {baseline.p95_ms:.0f} ms "
                           "for the current version")
    if reasons:
        return Verdict(False, reasons)
    return Verdict(True, [f"{canary.requests} requests, {canary.error_rate:.1%} errors, "
                          f"p95 {canary.p95_ms:.0f} ms"])


def p95(latencies_ms: list[float]) -> float:
    if not latencies_ms:
        return 0.0
    ordered = sorted(latencies_ms)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
