"""Decide whether a pull request can merge without a person.

The pipeline merges automatically only when the change is low-risk *and*
every required check has passed. Anything risky waits for a human, and the PR
gets a comment saying exactly why. The rules are deliberately boring and
explicit: a merge decision must be explainable after the fact.

Input is the PR as the GitHub API describes it (see `PullRequest.from_github`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch

# Paths where a mistake has a wide blast radius: always need a human.
SENSITIVE_PATHS = (".github/workflows/*", "infra/*", "services/shared/*", "services.yaml",
                   "autopilot/deploy/*", "autopilot/merge_policy.py")
BOT_AUTHORS = ("dependabot[bot]", "renovate[bot]")
LARGE_CHANGE_LINES = 400
AUTOMERGE_LABEL = "automerge"
BLOCK_LABELS = ("do-not-merge", "wip", "needs-review")


class Action(StrEnum):
    MERGE = "merge"      # all clear: merge now
    WAIT = "wait"        # not yet: checks running, or a person must approve
    BLOCK = "block"      # cannot merge as it stands


@dataclass
class PullRequest:
    number: int
    author: str
    draft: bool
    labels: list[str]
    approvals: int
    changes_requested: bool
    mergeable: bool | None          # None while GitHub is still computing it
    files: list[str]
    additions: int
    deletions: int
    checks: dict[str, str]          # check name -> success|failure|pending|skipped|...
    title: str = ""

    @classmethod
    def from_github(cls, pr: dict, files: list[dict], reviews: list[dict],
                    checks: dict[str, str]) -> PullRequest:
        latest: dict[str, str] = {}
        for review in reviews:  # chronological; keep each reviewer's latest state
            latest[review["user"]["login"]] = review["state"]
        return cls(
            number=pr["number"], author=pr["user"]["login"], draft=pr.get("draft", False),
            labels=[label["name"] for label in pr.get("labels", [])],
            approvals=sum(1 for s in latest.values() if s == "APPROVED"),
            changes_requested=any(s == "CHANGES_REQUESTED" for s in latest.values()),
            mergeable=pr.get("mergeable"), files=[f["filename"] for f in files],
            additions=pr.get("additions", 0), deletions=pr.get("deletions", 0),
            checks=checks, title=pr.get("title", ""),
        )


@dataclass
class Decision:
    action: Action
    risk: str                       # low | medium | high
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        icon = {"merge": "✅", "wait": "⏳", "block": "⛔"}[self.action]
        lines = [f"{icon} **Auto-merge: {self.action}** (risk: {self.risk})", ""]
        lines += [f"- {r}" for r in self.reasons]
        return "\n".join(lines)


def assess_risk(pr: PullRequest) -> tuple[str, list[str]]:
    reasons = []
    sensitive = sorted({f for f in pr.files for p in SENSITIVE_PATHS if fnmatch(f, p)})
    if sensitive:
        reasons.append(f"touches sensitive paths: {', '.join(sensitive[:5])}")
    size = pr.additions + pr.deletions
    if size > LARGE_CHANGE_LINES:
        reasons.append(f"large change ({size} lines > {LARGE_CHANGE_LINES})")
    if reasons:
        return "high", reasons

    docs_only = pr.files and all(f.endswith((".md", ".png", ".svg")) or f.startswith("docs/")
                                 for f in pr.files)
    tests_only = pr.files and all("/tests/" in f or f.startswith("tests/") for f in pr.files)
    if docs_only:
        return "low", ["documentation only"]
    if tests_only:
        return "low", ["tests only"]
    if pr.author in BOT_AUTHORS:
        return "low", [f"dependency update from {pr.author}"]
    return "medium", ["application code change"]


def decide(pr: PullRequest, required_checks: list[str]) -> Decision:
    # Hard stops first: these are never overridden.
    blockers = []
    if pr.draft:
        blockers.append("pull request is a draft")
    if hit := sorted(set(pr.labels) & set(BLOCK_LABELS)):
        blockers.append(f"labelled {', '.join(hit)}")
    if pr.changes_requested:
        blockers.append("a reviewer requested changes")
    if pr.mergeable is False:
        blockers.append("merge conflicts with the base branch")
    failed = sorted(c for c in required_checks if pr.checks.get(c) == "failure")
    if failed:
        blockers.append(f"required checks failed: {', '.join(failed)}")

    risk, risk_reasons = assess_risk(pr)
    if blockers:
        return Decision(Action.BLOCK, risk, blockers)

    waiting = []
    missing = sorted(c for c in required_checks if pr.checks.get(c) not in ("success", "skipped"))
    if missing:
        waiting.append(f"waiting for checks: {', '.join(missing)}")
    if pr.mergeable is None:
        waiting.append("GitHub is still computing mergeability")

    # Who has to say yes, by risk.
    opted_in = AUTOMERGE_LABEL in pr.labels or pr.author in BOT_AUTHORS
    if risk == "high" and pr.approvals < 1:
        waiting.append("high-risk change needs a human approval")
    elif risk == "medium" and not (pr.approvals >= 1 or opted_in):
        waiting.append(f"needs an approval or the '{AUTOMERGE_LABEL}' label")

    if waiting:
        return Decision(Action.WAIT, risk, waiting + [f"risk: {r}" for r in risk_reasons])

    why = [f"all {len(required_checks)} required checks passed"] + [
        f"risk: {r}" for r in risk_reasons]
    if pr.approvals:
        why.append(f"{pr.approvals} approval(s)")
    elif opted_in:
        why.append(f"opted in via '{AUTOMERGE_LABEL}'" if AUTOMERGE_LABEL in pr.labels
                   else "trusted dependency bot")
    return Decision(Action.MERGE, risk, why)
