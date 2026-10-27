# Scenario: orders-flaky-warmup

> Orders rejects the first request after start-up while it warms up. Whichever integration test runs first fails, then passes on retry. Nothing changed: the agent should call it a flake and name the race, not blame a commit.

## 🔎 Root cause: `orders` — flaky test

**flaky: tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_retrievable failed once (orders returned HTTP 503 once), then passed on retry**

Confidence 75% · investigated by **playbook** (runtime specialist) · 4 tool calls


### What to do

1. Quarantine tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_retrievable and file a fix; it is passing on retry, not reliably.
2. Look for a start-up race in orders: the first request after start-up failed. Gate traffic on a readiness check that reflects warm-up.

### Evidence

| | What | Where |
|---|---|---|
| E3 | tests flaky_test: tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_retrievable failed (AssertionError: {"error":"warming up"} assert 503 == 201  +  where 503 = <Response [503 Service Unavailable]>.status_cod) then passed on retry | `junit/integration.xml` |
| E4 | orders http_error: HTTP 503 | `traces/orders.jsonl:4` |
| E5 | tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_retrievable: AssertionError: {"error":"warming up"} assert 503 == 201  +  where 503 = <Response [503 Service Unavailable]>.status_code | `junit/integration.xml` |

<details><summary>How this was investigated (5 steps)</summary>

1. `pipeline_overview()` → {"stages": [{"stage": "integration-tests", "kind": "integration", "status": "success", "service": null, "evidence": "E1"}], "failed_tests": [], "flaky_tests": [
2. `rank_root_causes()` → {"hypotheses": [{"service": "orders", "category": "flaky_test", "summary": "flaky: tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_retrieva
3. `get_test_failure(test_id=tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_ret)` → {"test": "tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_retrievable", "outcome": "failed", "passed_on_retry": true, "message": "Assertion
4. `recent_changes(service=orders)` → {"service": "orders", "commits": [], "note": "no changes to this service since the last good build"}
5. `submit_diagnosis(root_cause_service=orders)` → accepted

</details>
