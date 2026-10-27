"""Run the services locally as real processes, the way the pipeline's
integration and canary stages do.

Every process writes its spans, structured logs and raw stdout/stderr into an
evidence directory. That directory is what the root-cause agents read later,
so nobody has to open a terminal or a log viewer to find out what happened.

Faults are injected through environment variables (see the services) or by
withholding required configuration, which reproduces real failure modes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from autopilot.registry import ROOT, Registry

# Configuration the services need to start. A scenario can withhold a key to
# reproduce a missing-config deployment.
BASE_CONFIG = {
    "inventory": {"INVENTORY_DB_URL": "sqlite:///inventory.db"},
}


@dataclass
class StackOptions:
    evidence: Path
    faults: dict[str, str] = field(default_factory=dict)   # service -> fault name
    withhold: dict[str, list[str]] = field(default_factory=dict)  # service -> env keys
    startup_timeout_s: float = 15.0


def _env_for(name: str, registry: Registry, opts: StackOptions) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_FAULT")}
    env.update(BASE_CONFIG.get(name, {}))
    for key in opts.withhold.get(name, []):
        env.pop(key, None)
    for dep in registry[name].depends_on:
        env[f"{dep.upper()}_URL"] = registry[dep].url
    if name in opts.faults:
        env[f"{name.upper()}_FAULT"] = opts.faults[name]
    env["TRACE_FILE"] = str(opts.evidence / "traces" / f"{name}.jsonl")
    env["LOG_FILE"] = str(opts.evidence / "logs" / f"{name}.jsonl")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def start_service(name: str, registry: Registry, opts: StackOptions,
                  mode: str = "w") -> subprocess.Popen:
    svc = registry[name]
    out = open(opts.evidence / "process" / f"{name}.log", mode, encoding="utf-8")  # noqa: SIM115
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"{name}.app:app",
         "--app-dir", str(ROOT / "services"),
         "--host", "127.0.0.1", "--port", str(svc.port), "--log-level", "warning"],
        env=_env_for(name, registry, opts), stdout=out, stderr=subprocess.STDOUT,
        cwd=opts.evidence, start_new_session=True,
    )


def wait_healthy(procs: dict[str, subprocess.Popen], registry: Registry,
                 timeout_s: float) -> dict[str, dict]:
    """healthy | crashed (process exited) | unhealthy (no 200 before timeout)."""
    status: dict[str, dict] = {}
    deadline = time.monotonic() + timeout_s
    pending = set(procs)
    while pending and time.monotonic() < deadline:
        for name in sorted(pending):
            proc = procs[name]
            if proc.poll() is not None:
                status[name] = {"status": "crashed", "exit_code": proc.returncode}
                pending.discard(name)
                continue
            try:
                if httpx.get(f"{registry[name].url}/healthz", timeout=0.5).status_code == 200:
                    status[name] = {"status": "healthy"}
                    pending.discard(name)
            except httpx.HTTPError:
                pass
        if pending:
            time.sleep(0.2)
    for name in pending:
        status[name] = {"status": "unhealthy"}
    return status


def stop_service(pid: int, grace_s: float = 3.0) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def up(registry: Registry, opts: StackOptions) -> dict:
    """Start every service (dependencies first) and wait for health checks.
    Never raises for a service that fails to start: that is evidence, and it
    is recorded in stack.json for the diagnosis."""
    for sub in ("traces", "logs", "process"):
        (opts.evidence / sub).mkdir(parents=True, exist_ok=True)

    state: dict = {"started_at": time.time(), "faults": opts.faults,
                   "withheld_config": opts.withhold, "services": {}}
    procs = {name: start_service(name, registry, opts) for name in registry.deploy_order()}
    health = wait_healthy(procs, registry, opts.startup_timeout_s)
    for name, proc in procs.items():
        state["services"][name] = {"pid": proc.pid, "port": registry[name].port, **health[name]}
    save_state(opts.evidence, state)
    return state


def load_state(evidence: Path) -> dict:
    return json.loads((evidence / "stack.json").read_text())


def save_state(evidence: Path, state: dict) -> None:
    (evidence / "stack.json").write_text(json.dumps(state, indent=2))


def down(evidence: Path) -> None:
    if not (evidence / "stack.json").exists():
        return
    for info in load_state(evidence)["services"].values():
        stop_service(info["pid"])
