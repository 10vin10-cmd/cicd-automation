"""What changed, and what it can break.

The pipeline builds and unit-tests only services whose code changed, but runs
integration tests for everything that *calls* them too: a change to payments
can break orders without touching a line of orders. A change to shared code
affects every service.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field

from autopilot.registry import ROOT, Registry


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    files: list[str]


@dataclass
class ChangeSet:
    base: str
    head: str
    files: list[str]
    changed_services: list[str]          # their own code changed
    impacted_services: list[str]         # changed + everything that calls them
    shared_changed: bool
    other_files: list[str] = field(default_factory=list)  # docs, tooling, CI
    commits: dict[str, list[Commit]] = field(default_factory=dict)  # per service

    def to_dict(self) -> dict:
        return asdict(self)


def classify(files: list[str], registry: Registry) -> tuple[set[str], bool, list[str]]:
    changed: set[str] = set()
    shared = False
    other: list[str] = []
    for path in files:
        if registry.is_shared_path(path):
            shared = True
        elif (svc := registry.service_for_path(path)) is not None:
            changed.add(svc)
        else:
            other.append(path)
    if shared:
        changed = set(registry.services)
    return changed, shared, other


def impacted(changed: set[str], registry: Registry) -> set[str]:
    out = set(changed)
    for svc in changed:
        out |= registry.callers_of(svc)
    return out


def detect(base: str, head: str = "HEAD", registry: Registry | None = None,
           cwd=ROOT) -> ChangeSet:
    registry = registry or Registry.load()
    if _resolves(cwd, base):
        files = _git(cwd, "diff", "--name-only", f"{base}...{head}").split()
    else:
        # No usable base (first push to a branch, or history not fetched):
        # treat everything as changed rather than silently testing nothing.
        files = _git(cwd, "ls-files").split()
        base = _git(cwd, "rev-list", "--max-parents=0", head).split()[0]
    changed, shared, other = classify(files, registry)
    commits = {svc: _commits(cwd, base, head, registry[svc].path) for svc in sorted(changed)}
    if shared:
        shared_commits = [c for p in registry.shared_paths for c in _commits(cwd, base, head, p)]
        for svc in commits:
            commits[svc] = _dedupe(commits[svc] + shared_commits)
    return ChangeSet(
        base=base, head=head, files=files,
        changed_services=sorted(changed),
        impacted_services=sorted(impacted(changed, registry)),
        shared_changed=shared, other_files=other, commits=commits,
    )


def _commits(cwd, base: str, head: str, path: str) -> list[Commit]:
    sep = "\x1f"
    out = _git(cwd, "log", f"--format=%H{sep}%an{sep}%aI{sep}%s", "--name-only",
               f"{base}..{head}", "--", path)
    # Each commit is a header line (containing the separator) followed, after
    # a blank line, by the files it touched.
    commits: list[Commit] = []
    for line in out.splitlines():
        if sep in line:
            sha, author, date, subject = line.split(sep, 3)
            commits.append(Commit(sha=sha, author=author, date=date, subject=subject, files=[]))
        elif line.strip() and commits:
            commits[-1].files.append(line.strip())
    return commits


def _dedupe(commits: list[Commit]) -> list[Commit]:
    seen: dict[str, Commit] = {}
    for c in commits:
        seen.setdefault(c.sha, c)
    return sorted(seen.values(), key=lambda c: c.date, reverse=True)


def _resolves(cwd, ref: str) -> bool:
    if not ref or set(ref) == {"0"}:
        return False
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          cwd=cwd, capture_output=True).returncode == 0


def _git(cwd, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout
