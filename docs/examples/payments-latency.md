# Scenario: payments-latency

> Payments adds a synchronous fraud-screening step that makes each charge take about 2.5 s. Orders waits 1 s for payments, so checkouts time out with 504.

## 🔎 Root cause: `payments` — latency

**payments is too slow for its callers: payments POST /charges took 2503 ms; orders gave up after 1005 ms; changed in 2c4e6a8 "Screen charges with fraud service before capture"**

Confidence 80% · investigated by **playbook** (runtime specialist) · 5 tool calls

**Impact:** orders → payments (the failure started in `payments`)
**Not the cause:** `orders` returned errors because calls to payments failed (timeout)
**Suspect commits:** `2c4e6a8b0d`

### What to do

1. Profile payments: its responses now exceed callers' timeouts.
2. If 2c4e6a8 ("Screen charges with fraud service before capture") is the cause, roll it back; otherwise review the timeout and retry budget between the services.

### Evidence

| | What | Where |
|---|---|---|
| E4 | payments timeout: payments POST /charges took 2503 ms; orders gave up after 1005 ms (x4) | `traces/orders.jsonl:3` |
| E5 | 4 failed request(s): orders -> payments; payments timeout: payments POST /charges took 2503 ms; orders gave up after 1005 ms | `traces/orders.jsonl:3` |
| E6 | commit 2c4e6a8 "Screen charges with fraud service before capture" touched payments | `changes.json` |

<details><summary>How this was investigated (6 steps)</summary>

1. `pipeline_overview()` → {"stages": [{"stage": "integration-tests", "kind": "integration", "status": "failure", "service": null, "evidence": "E1"}], "failed_tests": [{"test": "tests/int
2. `rank_root_causes()` → {"hypotheses": [{"service": "payments", "category": "latency", "summary": "payments is too slow for its callers: payments POST /charges took 2503 ms; orders gav
3. `trace_errors(service=payments)` → {"failure_chains": [{"failing_service": "payments", "what_failed": "timeout", "detail": "payments POST /charges took 2503 ms; orders gave up after 1005 ms", "op
4. `log_patterns(service=payments)` → {"service": "payments", "warning_or_error_lines": 0, "patterns": []}
5. `recent_changes(service=payments)` → {"service": "payments", "commits": [{"sha": "2c4e6a8b0d", "author": "dev-payments", "date": "2026-10-27T11:05:00+05:30", "subject": "Screen charges with fraud s
6. `submit_diagnosis(root_cause_service=payments)` → accepted

</details>
