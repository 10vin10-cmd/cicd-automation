# Scenario: build-dependency-conflict

> A dependency bump pins starlette to a version fastapi doesn't support. The payments build fails during dependency resolution. The build log is a real pip run, recorded in recorded/build-dependency-conflict.

## 🔎 Root cause: `payments` — build failure

**payments failed to build: Cannot install fastapi==0.115.0 and starlette==0.47.0 because these package versions have conflicting dependencies; changed in b1d3f5a "Bump starlette to 0.47.0"**

Confidence 90% · investigated by **playbook** (build specialist) · 4 tool calls

**Suspect commits:** `b1d3f5a7c9`

### What to do

1. Fix the build of payments: Cannot install fastapi==0.115.0 and starlette==0.47.0 because these package versions have conflicting dependencies
2. Resolve the conflicting pins: The user requested starlette==0.47.0; fastapi 0.115.0 depends on starlette<0.39.0 and >=0.37.2

### Evidence

| | What | Where |
|---|---|---|
| E2 | payments dependency_conflict: Cannot install fastapi==0.115.0 and starlette==0.47.0 because these package versions have conflicting dependencies | `build/payments.log:9` |
| E3 | build log build-payments: ERROR: Cannot install fastapi==0.115.0 and starlette==0.47.0 because these package versions have conflicting dependencies. | `build/payments.log:4` |
| E4 | commit b1d3f5a "Bump starlette to 0.47.0" touched payments | `changes.json` |

<details><summary>How this was investigated (5 steps)</summary>

1. `pipeline_overview()` → {"stages": [{"stage": "build-payments", "kind": "build", "status": "failure", "service": "payments", "evidence": "E1"}], "failed_tests": [], "flaky_tests": [], 
2. `rank_root_causes()` → {"hypotheses": [{"service": "payments", "category": "build_failure", "summary": "payments failed to build: Cannot install fastapi==0.115.0 and starlette==0.47.0
3. `build_log(stage=build-payments)` → {"stage": "build-payments", "service": "payments", "excerpt": ["Collecting starlette==0.47.0", "  Downloading starlette-0.47.0-py3-none-any.whl.metadata (6.2 kB
4. `recent_changes(service=payments)` → {"service": "payments", "commits": [{"sha": "b1d3f5a7c9", "author": "dependency-bot", "date": "2026-10-27T08:00:00+05:30", "subject": "Bump starlette to 0.47.0"
5. `submit_diagnosis(root_cause_service=payments)` → accepted

</details>
