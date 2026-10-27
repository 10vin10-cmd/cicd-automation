"""pytest plugin: write the failed tests as absolute node ids, so a retry can
target exactly them no matter which directory pytest treated as its root.

Loaded with `-p autopilot.pytest_failed`; the output file is named by the
AUTOPILOT_FAILED_FILE environment variable.
"""

from __future__ import annotations

import json
import os

_items: dict[str, str] = {}
_failed: list[str] = []


def pytest_collection_modifyitems(items):
    for item in items:
        parts = item.nodeid.split("::", 1)
        _items[item.nodeid] = f"{item.path}::{parts[1]}" if len(parts) == 2 else str(item.path)


def pytest_runtest_logreport(report):
    if report.failed:
        target = _items.get(report.nodeid, report.nodeid)
        if target not in _failed:
            _failed.append(target)


def pytest_sessionfinish(session):
    path = os.environ.get("AUTOPILOT_FAILED_FILE")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_failed, f)
