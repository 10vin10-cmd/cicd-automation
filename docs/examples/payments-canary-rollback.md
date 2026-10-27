# Scenario: payments-canary-rollback

> Integration tests passed against the old version, then the new payments build is deployed to staging. Under canary traffic it responds too slowly, the canary fails, and the deployment is rolled back automatically.

## 🔎 Root cause: `payments` — latency

**payments is too slow for its callers: payments POST /charges took 2502 ms; orders gave up after 1006 ms; changed in 7e5d3c1 "Call ledger synchronously when capturing a charge"**

Confidence 80% · investigated by **playbook** (runtime specialist) · 6 tool calls

**Impact:** orders → payments (the failure started in `payments`)
**Not the cause:** `orders` returned errors because calls to payments failed (timeout)
**Suspect commits:** `7e5d3c1b9a`

### What to do

1. Profile payments: its responses now exceed callers' timeouts.
2. If 7e5d3c1 ("Call ledger synchronously when capturing a charge") is the cause, roll it back; otherwise review the timeout and retry budget between the services.

### Evidence

| | What | Where |
|---|---|---|
| E3 | payments timeout: payments POST /charges took 2502 ms; orders gave up after 1006 ms (x20) | `traces/orders.jsonl:148` |
| E4 | 20 failed request(s): orders -> payments; payments timeout: payments POST /charges took 2502 ms; orders gave up after 1006 ms | `traces/orders.jsonl:148` |
| E5 | deploy payments v2.4.0: rolled_back; p95 1020 ms vs 11 ms for the current version; rolled back to current | `deploys.json` |
| E6 | commit 7e5d3c1 "Call ledger synchronously when capturing a charge" touched payments | `changes.json` |

<details><summary>How this was investigated (7 steps)</summary>

1. `pipeline_overview()` → {"stages": [{"stage": "integration-tests", "kind": "integration", "status": "success", "service": null, "evidence": "E1"}, {"stage": "deploy-staging-payments", 
2. `rank_root_causes()` → {"hypotheses": [{"service": "payments", "category": "latency", "summary": "payments is too slow for its callers: payments POST /charges took 2502 ms; orders gav
3. `trace_errors(service=payments)` → {"failure_chains": [{"failing_service": "payments", "what_failed": "timeout", "detail": "payments POST /charges took 2502 ms; orders gave up after 1006 ms", "op
4. `log_patterns(service=payments)` → {"service": "payments", "warning_or_error_lines": 0, "patterns": []}
5. `deploy_history()` → {"deploys": [{"service": "payments", "version": "v2.4.0", "previous_version": "current", "outcome": "rolled_back", "target": "local", "reasons": ["p95 1020 ms >
6. `recent_changes(service=payments)` → {"service": "payments", "commits": [{"sha": "7e5d3c1b9a", "author": "dev-payments", "date": "2026-10-27T12:30:00+05:30", "subject": "Call ledger synchronously w
7. `submit_diagnosis(root_cause_service=payments)` → accepted

</details>
