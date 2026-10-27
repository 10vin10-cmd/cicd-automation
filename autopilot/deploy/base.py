"""What every deploy target shares: the result record and the deploy history
that the root-cause agents read ("what was deployed just before this broke?")."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path


class Outcome(StrEnum):
    PROMOTED = "promoted"          # new version is live
    ROLLED_BACK = "rolled_back"    # new version failed checks; previous restored
    FAILED = "failed"              # failed and could not be rolled back: page someone


@dataclass
class DeployResult:
    service: str
    version: str
    previous_version: str | None
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    target: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.PROMOTED


def record(history_file: Path, result: DeployResult) -> None:
    """Append to the deploy history kept in the evidence directory."""
    history = json.loads(history_file.read_text()) if history_file.exists() else []
    result.finished_at = result.finished_at or time.time()
    history.append({**asdict(result), "outcome": result.outcome.value})
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps(history, indent=2))
