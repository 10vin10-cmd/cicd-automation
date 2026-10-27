from builders import EvidenceBuilder

from autopilot.doctor import analysis
from autopilot.doctor.evidence import Bundle

INTEG = "tests/integration/test_checkout_flow.py::test_order_is_confirmed_and_retrievable"


def load(b: EvidenceBuilder) -> Bundle:
    return Bundle.load(b.write())


def test_blames_the_deepest_failing_service_not_the_one_that_reported_it(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.failed_checkout_via_payments_exception()
    b.failed_checkout_via_payments_exception()
    b.stage(name="integration-tests", kind="integration", status="failure")
    top = analysis.hypotheses(load(b))[0]

    assert top.service == "payments"
    assert top.category == "code_regression"
    assert top.impact_path == ["orders", "payments"]
    assert "KeyError: 'amount'" in top.summary
    assert top.findings[0].count == 2


def test_orders_is_reported_as_a_symptom(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.failed_checkout_via_payments_exception()
    syms = analysis.symptoms(load(b), "payments")
    assert [s["service"] for s in syms] == ["orders"]


def test_4xx_answers_are_not_failures(tmp_path):
    # OpenTelemetry marks client spans as errors on 409; a stock-out is a correct answer.
    b = EvidenceBuilder(tmp_path)
    b.checkout_out_of_stock()
    assert analysis.trace_findings(load(b)) == []


def test_timeout_names_the_slow_service_with_both_timings(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.checkout_timeout()
    top = analysis.hypotheses(load(b))[0]
    assert (top.service, top.category) == ("payments", "latency")
    assert "took 2500 ms" in top.summary and "gave up after 1000 ms" in top.summary


def test_refused_connection_is_explained_by_a_start_up_crash(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.checkout_refused()
    b.crashed("inventory", "Traceback (most recent call last):\n  ...\nKeyError: 'INVENTORY_DB_URL'\n")
    hyps = analysis.hypotheses(load(b))
    assert len(hyps) == 1
    assert (hyps[0].service, hyps[0].category) == ("inventory", "config_error")
    assert "INVENTORY_DB_URL" in hyps[0].actions[0]


def test_crash_without_config_signal_is_a_code_regression(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.crashed("payments", "Traceback...\nAttributeError: 'NoneType' object has no attribute 'x'\n")
    assert analysis.hypotheses(load(b))[0].category == "code_regression"


def test_recent_commit_raises_confidence_and_is_named(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.failed_checkout_via_payments_exception()
    without = analysis.hypotheses(load(b))[0].confidence
    b.commit("payments", "abcdef1234567", "Rename amount field")
    top = analysis.hypotheses(load(b))[0]
    assert top.confidence > without
    assert 'abcdef1 "Rename amount field"' in top.summary
    assert "Revert or fix abcdef1" in top.actions[0]


def test_flake_only_run_is_called_flaky(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.junit("integration", [(INTEG, "assert 503 == 201")])
    b.junit("integration", [(INTEG, None)], retry=True)
    b.stage(name="integration-tests", kind="integration", status="success")
    hyps = analysis.hypotheses(load(b))
    assert [h.category for h in hyps] == ["flaky_test"]


def test_flake_is_secondary_to_a_hard_failure(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.failed_checkout_via_payments_exception()
    b.junit("integration", [(INTEG, "assert 503 == 201"), ("tests/integration/x.py::t2", "boom")])
    b.junit("integration", [(INTEG, None), ("tests/integration/x.py::t2", "boom")], retry=True)
    b.stage(name="integration-tests", kind="integration", status="failure")
    hyps = analysis.hypotheses(load(b))
    assert hyps[0].service == "payments"
    assert next(h for h in hyps if h.category == "flaky_test").confidence <= 0.3


def test_build_dependency_conflict(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.file("build/payments.log", "Collecting x\nERROR: Cannot install fastapi==1 and starlette==2 "
           "because these package versions have conflicting dependencies.\n\n"
           "The conflict is caused by:\n    The user requested starlette==2\n"
           "    fastapi 1 depends on starlette<1\n\nTo fix this you could try to:\n")
    b.stage(name="build-payments", kind="build", status="failure", service="payments",
            log="build/payments.log")
    top = analysis.hypotheses(load(b))[0]
    assert (top.service, top.category) == ("payments", "build_failure")
    assert "fastapi==1 and starlette==2" in top.summary
    assert top.actions[1] == ("Resolve the conflicting pins: The user requested starlette==2; "
                              "fastapi 1 depends on starlette<1")


def test_build_syntax_error_points_at_file_and_line(tmp_path):
    b = EvidenceBuilder(tmp_path)
    b.file("build/orders.log", '  File "services/orders/app.py", line 42\n    def f(:\n'
           "         ^\nSyntaxError: invalid syntax\n")
    b.stage(name="build-orders", kind="build", status="failure", service="orders",
            log="build/orders.log")
    top = analysis.hypotheses(load(b))[0]
    assert top.findings[0].kind == "syntax_error"
    assert top.findings[0].sources == ["build/orders.log:1"]


def test_unit_failures_belong_to_their_service(tmp_path):
    b = EvidenceBuilder(tmp_path)
    test = "services/orders/tests/test_orders_app.py::test_places_order"
    b.junit("unit-orders", [(test, "assert 502 == 201")])
    b.stage(name="unit-orders", kind="unit", status="failure", service="orders")
    b.commit("orders", "1234567abc", "Refactor order totals")
    top = analysis.hypotheses(load(b))[0]
    assert (top.service, top.category) == ("orders", "test_regression")


def test_evidence_paths_cannot_escape_the_bundle(tmp_path):
    b = EvidenceBuilder(tmp_path)
    bundle = load(b)
    try:
        bundle.read_log("../../etc/passwd")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal was allowed")
