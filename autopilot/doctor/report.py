"""Render a diagnosis for people: a pull-request comment / job summary in
Markdown, and JSON for anything downstream (chat-ops, ticketing, metrics)."""

from __future__ import annotations

import json

from autopilot.doctor.agents import Diagnosis
from autopilot.doctor.tools import Ledger

LABELS = {
    "code_regression": "code regression",
    "config_error": "configuration error",
    "dependency_unavailable": "dependency unavailable",
    "latency": "latency",
    "build_failure": "build failure",
    "test_regression": "unit test regression",
    "flaky_test": "flaky test",
    "unknown": "unknown",
}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(d: Diagnosis, ledger: Ledger, run_url: str | None = None) -> str:
    out = [f"## 🔎 Root cause: `{d.root_cause_service}` — {LABELS.get(d.category, d.category)}",
           "", f"**{d.summary}**", ""]
    meta = [f"Confidence {d.confidence:.0%}",
            f"investigated by **{d.agent}** ({d.specialist} specialist)",
            f"{sum(1 for s in d.steps if s.tool != 'submit_diagnosis')} tool calls"]
    if run_url:
        meta.append(f"[pipeline run]({run_url})")
    out += [" · ".join(meta), ""]

    if len(d.impact_path) > 1:
        out.append(f"**Impact:** {' → '.join(d.impact_path)} (the failure started in "
                   f"`{d.impact_path[-1]}`)")
    for nc in d.not_the_cause:
        out.append(f"**Not the cause:** `{nc['service']}` {nc['why']}")
    if d.suspect_commits:
        out.append(f"**Suspect commits:** {', '.join(f'`{c}`' for c in d.suspect_commits)}")
    out.append("")

    out += ["### What to do", ""] + [f"{i}. {a}" for i, a in enumerate(d.actions, 1)] + [""]

    out += ["### Evidence", "", "| | What | Where |", "|---|---|---|"]
    for eid in d.evidence:
        item = ledger.items.get(eid)
        if item:
            out.append(f"| {eid} | {_cell(item.summary)} | `{_cell(item.source)}` |")
    out.append("")

    for note in d.notes:
        out.append(f"> {note}")
    if d.notes:
        out.append("")

    out += [f"<details><summary>How this was investigated ({len(d.steps)} steps)</summary>", ""]
    for i, s in enumerate(d.steps, 1):
        args = ", ".join(f"{k}={v}" for k, v in s.args.items()) if s.args else ""
        flag = " ⚠️" if s.error else ""
        out.append(f"{i}. `{s.tool}({_cell(args)[:80]})`{flag} → {_cell(s.result_summary)[:160]}")
    if d.usage.get("turns"):
        out.append("")
        out.append(f"Model usage: {d.usage['turns']} turns, {d.usage['input_tokens']:,} input / "
                   f"{d.usage['output_tokens']:,} output tokens.")
    out += ["", "</details>"]
    return "\n".join(out)


def as_json(d: Diagnosis, ledger: Ledger) -> str:
    doc = d.to_dict()
    doc["evidence"] = [{"id": e, "summary": ledger.items[e].summary,
                        "source": ledger.items[e].source} for e in d.evidence if e in ledger.items]
    return json.dumps(doc, indent=2, default=str)
