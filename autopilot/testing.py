"""Run a test suite the way the pipeline does: JUnit report into the evidence
directory, then one retry of only the failed tests.

A test that fails and then passes on retry is recorded as flaky. The stage
still passes (a flake shouldn't block a release) but the flake is reported,
because flakes hide real failures over time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SuiteResult:
    name: str
    passed: bool
    failed_first: list[str]
    failed_after_retry: list[str]

    @property
    def flaky(self) -> list[str]:
        return sorted(set(self.failed_first) - set(self.failed_after_retry))


def run_suite(name: str, paths: list[str], evidence: Path, retry: bool = True,
              extra_args: list[str] | None = None) -> SuiteResult:
    junit_dir = evidence / "junit"
    junit_dir.mkdir(parents=True, exist_ok=True)
    first = junit_dir / f"{name}.xml"
    code, targets = _pytest(paths, first, extra_args)
    failed_first = failed_tests(first)
    if code not in (0, 1) and not failed_first:
        # Collection or usage error: nothing ran, which is not a pass.
        failed_first = [f"{name}: pytest exited with code {code} before running tests"]
        return SuiteResult(name, False, failed_first, failed_first)

    failed_after = failed_first
    if failed_first and retry and targets:
        rerun = junit_dir / f"{name}.retry.xml"
        retry_code, _ = _pytest(targets, rerun, extra_args)
        failed_after = failed_tests(rerun)
        if retry_code not in (0, 1) or (retry_code == 1 and not failed_after):
            failed_after = failed_first  # the retry itself didn't run properly

    return SuiteResult(name=name, passed=not failed_after, failed_first=failed_first,
                       failed_after_retry=failed_after)


def failed_tests(junit_file: Path) -> list[str]:
    """Node ids (path::name) of failed or errored tests in a JUnit report."""
    if not junit_file.exists():
        return []
    out = []
    for case in ET.parse(junit_file).getroot().iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            out.append(node_id(case))
    return out


def node_id(case: ET.Element) -> str:
    classname = case.get("classname", "")
    file = case.get("file") or classname.replace(".", "/") + ".py"
    return f"{file}::{case.get('name')}"


def _pytest(targets: list[str], junit: Path,
            extra_args: list[str] | None) -> tuple[int, list[str]]:
    """Run pytest; return its exit code and the failed tests as absolute node
    ids (from the autopilot.pytest_failed plugin) for an exact retry."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        failed_file = tmp.name
    try:
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "-p", "autopilot.pytest_failed", f"--junitxml={junit}",
               "-o", "junit_family=xunit2", *(extra_args or []), *targets]
        env = {**os.environ, "AUTOPILOT_FAILED_FILE": failed_file}
        code = subprocess.run(cmd, capture_output=True, text=True, env=env).returncode
        text = Path(failed_file).read_text()
        return code, json.loads(text) if text.strip() else []
    finally:
        Path(failed_file).unlink(missing_ok=True)
