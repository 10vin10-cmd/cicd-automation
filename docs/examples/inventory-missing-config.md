# Scenario: inventory-missing-config

> Inventory now reads its database URL from the environment, but the setting was never added to the deployment. Inventory crashes on start-up; orders can't reach it, and every checkout fails with 503.

## 🔎 Root cause: `inventory` — configuration error

**inventory crashed on start-up (KeyError: 'INVENTORY_DB_URL'), so calls from orders were refused; changed in 9a1b7c3 "Read database URL from INVENTORY_DB_URL"**

Confidence 92% · investigated by **playbook** (runtime specialist) · 6 tool calls

**Impact:** orders → inventory (the failure started in `inventory`)
**Not the cause:** `orders` returned errors because calls to inventory failed (unreachable)
**Suspect commits:** `9a1b7c3d5e`

### What to do

1. Set `INVENTORY_DB_URL` for inventory in its deployment configuration.
2. Validate required configuration in the deploy stage, before traffic shifts.

### Evidence

| | What | Where |
|---|---|---|
| E6 | inventory startup_crash: KeyError: 'INVENTORY_DB_URL' | `process/inventory.log:47` |
| E7 | inventory unreachable: ConnectError: [Errno 111] Connection refused (x6) | `traces/orders.jsonl:2` |
| E8 | 6 failed request(s): orders -> inventory; inventory unreachable: ConnectError: [Errno 111] Connection refused | `traces/orders.jsonl:2` |
| E9 | inventory crashed on start-up: KeyError: 'INVENTORY_DB_URL' | `process/inventory.log:47` |
| E10 | commit 9a1b7c3 "Read database URL from INVENTORY_DB_URL" touched inventory | `changes.json` |

<details><summary>How this was investigated (7 steps)</summary>

1. `pipeline_overview()` → {"stages": [{"stage": "integration-tests", "kind": "integration", "status": "failure", "service": null, "evidence": "E1"}], "failed_tests": [{"test": "tests/int
2. `rank_root_causes()` → {"hypotheses": [{"service": "inventory", "category": "config_error", "summary": "inventory crashed on start-up (KeyError: 'INVENTORY_DB_URL'), so calls from ord
3. `trace_errors(service=inventory)` → {"failure_chains": [{"failing_service": "inventory", "what_failed": "unreachable", "detail": "ConnectError: [Errno 111] Connection refused", "operation": "http:
4. `process_status()` → {"processes": {"inventory": {"status": "crashed", "exit_code": 1, "error": "KeyError: 'INVENTORY_DB_URL'", "output_tail": "  File \"<frozen importlib._bootstrap
5. `log_patterns(service=inventory)` → {"service": "inventory", "warning_or_error_lines": 0, "patterns": []}
6. `recent_changes(service=inventory)` → {"service": "inventory", "commits": [{"sha": "9a1b7c3d5e", "author": "dev-fulfilment", "date": "2026-10-27T09:40:00+05:30", "subject": "Read database URL from I
7. `submit_diagnosis(root_cause_service=inventory)` → accepted

</details>
