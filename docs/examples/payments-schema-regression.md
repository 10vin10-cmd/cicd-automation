# Scenario: payments-schema-regression

> A refactor in payments renames the `amount_cents` field to `amount`. Payments' own tests still pass (they were updated with it), but orders still sends `amount_cents`. Checkout integration tests fail with 502 from orders.

## 🔎 Root cause: `payments` — code regression

**payments raises KeyError: 'amount' in POST /charges; changed in 4f9c2e1 "Rename amount_cents to amount in charge API"**

Confidence 88% · investigated by **playbook** (runtime specialist) · 5 tool calls

**Impact:** orders → payments (the failure started in `payments`)
**Not the cause:** `orders` returned errors because calls to payments failed (exception)
**Suspect commits:** `4f9c2e1ab7`

### What to do

1. Revert or fix 4f9c2e1 ("Rename amount_cents to amount in charge API") in payments.
2. Add a contract test between orders and payments so an incompatible API change fails in the payments pipeline.

### Evidence

| | What | Where |
|---|---|---|
| E4 | payments exception: KeyError: 'amount' (x4) | `traces/payments.jsonl:4` |
| E5 | 4 failed request(s): orders -> payments; payments exception: KeyError: 'amount' | `traces/payments.jsonl:4` |
| E6 | payments log x4: unhandled error on POST /charges \| KeyError: 'amount' | `logs/payments.jsonl:1` |
| E7 | commit 4f9c2e1 "Rename amount_cents to amount in charge API" touched payments | `changes.json` |

<details><summary>How this was investigated (6 steps)</summary>

1. `pipeline_overview()` → {"stages": [{"stage": "integration-tests", "kind": "integration", "status": "failure", "service": null, "evidence": "E1"}], "failed_tests": [{"test": "tests/int
2. `rank_root_causes()` → {"hypotheses": [{"service": "payments", "category": "code_regression", "summary": "payments raises KeyError: 'amount' in POST /charges; changed in 4f9c2e1 \"Ren
3. `trace_errors(service=payments)` → {"failure_chains": [{"failing_service": "payments", "what_failed": "exception", "detail": "KeyError: 'amount'", "operation": "POST /charges", "requests_affected
4. `log_patterns(service=payments)` → {"service": "payments", "warning_or_error_lines": 4, "patterns": [{"pattern": "unhandled error on POST /charges \| KeyError: 'amount'", "count": 4, "level": "ER
5. `recent_changes(service=payments)` → {"service": "payments", "commits": [{"sha": "4f9c2e1ab7", "author": "dev-payments", "date": "2026-10-27T10:12:00+05:30", "subject": "Rename amount_cents to amou
6. `submit_diagnosis(root_cause_service=payments)` → accepted

</details>
