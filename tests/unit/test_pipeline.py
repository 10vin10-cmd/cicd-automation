import json
import subprocess
from pathlib import Path

import pytest

from autopilot.changes import classify, detect, impacted
from autopilot.deploy.canary import Sample, Thresholds, analyse, p95
from autopilot.merge_policy import Action, PullRequest, decide
from autopilot.registry import Registry
from autopilot.testing import run_suite

REGISTRY = Registry.load()


# -- registry ----------------------------------------------------------------------

def test_dependencies_deploy_before_their_callers():
    assert REGISTRY.deploy_order() == ["inventory", "payments", "orders"]


def test_callers_and_dependencies():
    assert REGISTRY.callers_of("payments") == {"orders"}
    assert REGISTRY.dependencies_of("orders") == {"inventory", "payments"}


def test_cycles_are_rejected(tmp_path):
    f = tmp_path / "services.yaml"
    f.write_text("services:\n  a: {path: a, port: 1, depends_on: [b]}\n"
                 "  b: {path: b, port: 2, depends_on: [a]}\n")
    with pytest.raises(ValueError, match="cycle"):
        Registry.load(f)


def test_unknown_dependency_is_rejected(tmp_path):
    f = tmp_path / "services.yaml"
    f.write_text("services:\n  a: {path: a, port: 1, depends_on: [ghost]}\n")
    with pytest.raises(ValueError, match="ghost"):
        Registry.load(f)


# -- change detection ----------------------------------------------------------------

def test_a_change_to_payments_also_tests_its_callers():
    changed, shared, other = classify(["services/payments/app.py", "README.md"], REGISTRY)
    assert changed == {"payments"} and not shared and other == ["README.md"]
    assert impacted(changed, REGISTRY) == {"payments", "orders"}


def test_shared_code_affects_everything():
    changed, shared, _ = classify(["services/shared/telemetry.py"], REGISTRY)
    assert shared and changed == {"orders", "inventory", "payments"}


def test_path_prefixes_are_exact():
    assert REGISTRY.service_for_path("services/ordersX/app.py") is None


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin"})


def test_detect_reads_commits_per_service(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    for svc in ("orders", "payments", "inventory"):
        (tmp_path / "services" / svc).mkdir(parents=True)
        (tmp_path / "services" / svc / "app.py").write_text("v1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "tag", "base")
    (tmp_path / "services/payments/app.py").write_text("v2\n")
    _git(tmp_path, "commit", "-qam", "Rename amount field")
    (tmp_path / "docs.md").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Docs")

    cs = detect("base", "HEAD", REGISTRY, cwd=tmp_path)
    assert cs.changed_services == ["payments"]
    assert cs.impacted_services == ["orders", "payments"]
    assert cs.other_files == ["docs.md"]
    assert [c.subject for c in cs.commits["payments"]] == ["Rename amount field"]
    assert cs.commits["payments"][0].files == ["services/payments/app.py"]


def test_missing_base_means_everything_changed(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "services" / "orders").mkdir(parents=True)
    (tmp_path / "services/orders/app.py").write_text("v1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "first")
    cs = detect("0" * 40, "HEAD", REGISTRY, cwd=tmp_path)
    assert cs.changed_services == ["orders"]
    assert [c.subject for c in cs.commits["orders"]] == []  # base is the root commit


# -- merge policy ------------------------------------------------------------------

CHECKS = {"build": "success", "unit": "success", "integration": "success"}


def pr(**kw) -> PullRequest:
    base = dict(number=7, author="dev", draft=False, labels=[], approvals=0,
                changes_requested=False, mergeable=True, files=["services/orders/app.py"],
                additions=20, deletions=5, checks=dict(CHECKS))
    return PullRequest(**{**base, **kw})


def test_low_risk_docs_change_merges_on_green():
    d = decide(pr(files=["docs/runbook.md"]), list(CHECKS))
    assert d.action == Action.MERGE and d.risk == "low"


def test_code_change_needs_approval_or_opt_in():
    assert decide(pr(), list(CHECKS)).action == Action.WAIT
    assert decide(pr(approvals=1), list(CHECKS)).action == Action.MERGE
    assert decide(pr(labels=["automerge"]), list(CHECKS)).action == Action.MERGE


def test_sensitive_paths_always_need_a_human():
    d = decide(pr(files=[".github/workflows/ci.yml"], labels=["automerge"]), list(CHECKS))
    assert d.action == Action.WAIT and d.risk == "high"
    assert any("sensitive" in r for r in d.reasons)


def test_large_changes_need_a_human():
    d = decide(pr(additions=900, labels=["automerge"]), list(CHECKS))
    assert d.action == Action.WAIT and d.risk == "high"


@pytest.mark.parametrize("kw,reason", [
    ({"draft": True}, "draft"),
    ({"labels": ["do-not-merge"]}, "do-not-merge"),
    ({"changes_requested": True}, "requested changes"),
    ({"mergeable": False}, "conflicts"),
    ({"checks": {**CHECKS, "integration": "failure"}}, "integration"),
])
def test_hard_blockers(kw, reason):
    d = decide(pr(approvals=2, **kw), list(CHECKS))
    assert d.action == Action.BLOCK
    assert any(reason in r for r in d.reasons)


def test_pending_checks_wait():
    d = decide(pr(approvals=1, checks={**CHECKS, "integration": "pending"}), list(CHECKS))
    assert d.action == Action.WAIT


def test_dependency_bot_counts_as_opt_in():
    d = decide(pr(author="dependabot[bot]", files=["services/orders/requirements.txt"]),
               list(CHECKS))
    assert d.action == Action.MERGE


def test_latest_review_per_reviewer_wins():
    reviews = [{"user": {"login": "a"}, "state": "APPROVED"},
               {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},
               {"user": {"login": "b"}, "state": "APPROVED"}]
    p = PullRequest.from_github({"number": 1, "user": {"login": "dev"}, "labels": [],
                                 "mergeable": True}, [{"filename": "x.py"}], reviews, {})
    assert p.approvals == 1 and p.changes_requested


# -- canary -----------------------------------------------------------------------

def test_healthy_canary_promotes():
    assert analyse(Sample(50, 0, 40), Sample(50, 0, 35)).promote


def test_error_spike_rolls_back():
    v = analyse(Sample(50, 10, 40), Sample(50, 0, 35))
    assert not v.promote and any("error rate" in r for r in v.reasons)


def test_latency_regression_rolls_back_even_under_the_absolute_limit():
    v = analyse(Sample(50, 0, 450), Sample(50, 0, 100), Thresholds(max_p95_ms=800))
    assert not v.promote and any("current version" in r for r in v.reasons)


def test_millisecond_jitter_is_not_a_regression():
    # 5 ms -> 9 ms is 80% slower but meaningless; it rolled back a healthy build in CI.
    assert analyse(Sample(30, 0, 9), Sample(30, 0, 5)).promote


def test_too_little_traffic_is_not_proof():
    assert not analyse(Sample(5, 0, 10)).promote


def test_p95():
    assert p95(list(range(1, 101))) == 95
    assert p95([]) == 0


# -- tests with retry -----------------------------------------------------------

def test_flaky_test_is_detected_and_does_not_fail_the_suite(tmp_path):
    marker = tmp_path / "ran-once"
    tests = tmp_path / "test_sample.py"
    tests.write_text(
        "from pathlib import Path\n"
        f"M = Path({str(marker)!r})\n"
        "def test_flaky():\n"
        "    first = not M.exists()\n"
        "    M.write_text('x')\n"
        "    assert not first\n"
        "def test_solid():\n"
        "    assert True\n")
    result = run_suite("sample", [str(tests)], tmp_path / "ev")
    assert result.passed
    assert len(result.flaky) == 1 and result.flaky[0].endswith("::test_flaky")
    assert (tmp_path / "ev" / "junit" / "sample.retry.xml").exists()


def test_real_failure_fails_after_retry(tmp_path):
    tests = tmp_path / "test_bad.py"
    tests.write_text("def test_bad():\n    assert 1 == 2\n")
    result = run_suite("bad", [str(tests)], tmp_path / "ev")
    assert not result.passed and result.failed_after_retry and not result.flaky


# -- CLI --------------------------------------------------------------------------

def test_cli_merge_check_and_stage(tmp_path, capsys, monkeypatch):
    from autopilot.cli import main
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    facts = tmp_path / "pr.json"
    facts.write_text(json.dumps(dict(number=3, author="dev", draft=False, labels=["automerge"],
                                     approvals=0, changes_requested=False, mergeable=True,
                                     files=["services/orders/app.py"], additions=3, deletions=1,
                                     checks={})))
    assert main(["merge-check", "--pr-json", str(facts), "--checks",
                 json.dumps({"unit": "success"})]) == 0
    assert "action=merge" in out_file.read_text()

    ev = tmp_path / "ev"
    main(["stage", "--evidence", str(ev), "--name", "build-orders", "--kind", "build",
          "--status", "failure", "--service", "orders"])
    main(["stage", "--evidence", str(ev), "--name", "build-orders", "--kind", "build",
          "--status", "success", "--service", "orders"])
    files = list((ev / "stages").glob("*.json"))
    assert [f.name for f in files] == ["build-orders.json"]  # re-recording replaces
    assert json.loads(files[0].read_text())["status"] == "success"
    from autopilot.doctor.evidence import Bundle
    assert [s.status for s in Bundle.load(ev).stages] == ["success"]


def test_cli_diagnose_offline(tmp_path, capsys):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from builders import EvidenceBuilder

    from autopilot.cli import main
    b = EvidenceBuilder(tmp_path / "ev")
    b.failed_checkout_via_payments_exception()
    b.stage(name="integration-tests", kind="integration", status="failure")
    b.write()
    report = tmp_path / "report.md"
    assert main(["diagnose", "--evidence", str(tmp_path / "ev"), "--offline",
                 "--out", str(report), "--json", str(tmp_path / "d.json")]) == 0
    assert "Root cause: `payments`" in report.read_text()
    assert json.loads((tmp_path / "d.json").read_text())["root_cause_service"] == "payments"
