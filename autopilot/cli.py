"""`autopilot`: the commands the pipeline runs. Each one is usable locally too.

    autopilot changes --base origin/main            what changed and what it affects
    autopilot merge-check --pr 42                   should this PR merge on its own?
    autopilot stack up|down --evidence ev/          run the services locally
    autopilot test integration tests/integration --evidence ev/
    autopilot stage --evidence ev/ --name build-payments --kind build --status failure ...
    autopilot deploy local --service payments --version $SHA --evidence ev/
    autopilot deploy ecs --cluster c --service s --container app --image repo:tag
    autopilot diagnose --evidence ev/ --out report.md
    autopilot scenario list|run NAME|eval
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from autopilot.registry import Registry


def _gh_output(**values) -> None:
    """Expose values to later GitHub Actions steps."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value if isinstance(value, str) else json.dumps(value)}\n")


def _summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


# -- commands ---------------------------------------------------------------------

def cmd_changes(args) -> int:
    from autopilot.changes import detect
    cs = detect(args.base, args.head)
    print(json.dumps(cs.to_dict(), indent=2, default=lambda o: o.__dict__))
    if args.write:
        Path(args.write).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write).write_text(json.dumps(cs.to_dict(), indent=2,
                                               default=lambda o: o.__dict__))
    _gh_output(changed=cs.changed_services, impacted=cs.impacted_services,
               any_service=str(bool(cs.impacted_services)).lower())
    return 0


def _gh_api(path: str) -> object:
    out = subprocess.run(["gh", "api", "--paginate", path], check=True, capture_output=True,
                         text=True).stdout
    # --paginate concatenates JSON arrays page by page
    return json.loads(out.replace("][", ","))


def cmd_merge_check(args) -> int:
    from autopilot.merge_policy import PullRequest, decide
    checks = json.loads(args.checks) if args.checks else {}
    if args.pr_json:
        facts = json.loads(Path(args.pr_json).read_text())
        pr = PullRequest(**facts)
        pr.checks = {**pr.checks, **checks}
    else:
        import time
        repo = os.environ["GITHUB_REPOSITORY"]
        base = f"repos/{repo}/pulls/{args.pr}"
        facts = _gh_api(base)
        for _ in range(5):  # GitHub computes mergeability lazily
            if facts.get("mergeable") is not None:
                break
            time.sleep(3)
            facts = _gh_api(base)
        pr = PullRequest.from_github(facts, _gh_api(f"{base}/files"),
                                     _gh_api(f"{base}/reviews"), checks)
    decision = decide(pr, required_checks=sorted(checks) if checks else args.required)
    print(decision.summary())
    _summary(decision.summary())
    _gh_output(action=decision.action.value, risk=decision.risk)
    return 0


def cmd_stack(args) -> int:
    from autopilot import stack
    evidence = Path(args.evidence).resolve()
    if args.action == "down":
        stack.down(evidence)
        return 0
    faults = dict(f.split("=", 1) for f in args.fault)
    withhold: dict[str, list[str]] = {}
    for w in args.withhold:
        svc, key = w.split("=", 1)
        withhold.setdefault(svc, []).append(key)
    state = stack.up(Registry.load(), stack.StackOptions(evidence, faults, withhold))
    for name, info in state["services"].items():
        print(f"{name:10} {info['status']:9} :{info['port']}")
    return 0 if all(i["status"] == "healthy" for i in state["services"].values()) else 1


def _record_stage(evidence: Path, **stage) -> None:
    """One file per stage, so evidence from parallel CI jobs merges cleanly."""
    d = evidence / "stages"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stage['name']}.json").write_text(json.dumps(stage, indent=2))


def cmd_test(args) -> int:
    from autopilot.testing import run_suite
    evidence = Path(args.evidence).resolve()
    result = run_suite(args.suite, args.paths, evidence, retry=not args.no_retry)
    _record_stage(evidence, name=f"{args.suite}-tests", kind=args.kind or args.suite,
                  status="success" if result.passed else "failure", service=args.service,
                  summary=f"failed: {result.failed_after_retry}; flaky: {result.flaky}")
    if result.flaky:
        print(f"⚠️ flaky (passed on retry): {', '.join(result.flaky)}")
        _gh_output(flaky="true")
    if result.failed_after_retry:
        print(f"❌ failed: {', '.join(result.failed_after_retry)}")
    return 0 if result.passed else 1


def cmd_stage(args) -> int:
    _record_stage(Path(args.evidence).resolve(), name=args.name, kind=args.kind,
                  status=args.status, service=args.service, log=args.log, summary=args.summary)
    return 0


def cmd_deploy(args) -> int:
    from autopilot.deploy.base import record
    if args.target == "local":
        from autopilot.deploy.local import Candidate, LocalDeployer
        evidence = Path(args.evidence).resolve()
        registry = Registry.load()
        deployer = LocalDeployer(registry, evidence)
        services = args.service or registry.deploy_order()
        faults = dict(f.split("=", 1) for f in (args.fault or []))
        ok = True
        for svc in [s for s in registry.deploy_order() if s in services]:
            result = deployer.deploy(svc, Candidate(version=args.version, fault=faults.get(svc)))
            print(f"{svc}: {result.outcome} :: {'; '.join(result.reasons)}")
            _record_stage(evidence, name=f"deploy-{args.environment}-{svc}", kind="deploy",
                          status="success" if result.ok else "failure", service=svc,
                          summary="; ".join(result.reasons))
            if not result.ok:
                ok = False
                break  # don't deploy callers on top of a failed dependency
        return 0 if ok else 1

    import boto3

    from autopilot.deploy.ecs import EcsDeployer, EcsTarget
    deployer = EcsDeployer(boto3.client("ecs"), boto3.client("cloudwatch"))
    target = EcsTarget(cluster=args.cluster, service=args.ecs_service, container=args.container,
                       alarms=args.alarm, bake_seconds=args.bake_seconds)
    result = deployer.deploy(target, image=args.image, version=args.version)
    if args.evidence:
        record(Path(args.evidence) / "deploys.json", result)
    print(f"{target.service}: {result.outcome} :: {'; '.join(result.reasons)}")
    return 0 if result.ok else 1


def cmd_diagnose(args) -> int:
    from autopilot.doctor import llm, report
    from autopilot.doctor.agents import diagnose
    from autopilot.doctor.evidence import Bundle
    bundle = Bundle.load(args.evidence)
    model = None if args.offline else llm.from_env()
    diagnosis, toolbox = diagnose(bundle, model)
    md = report.markdown(diagnosis, toolbox.ledger, run_url=args.run_url)
    print(md)
    if args.out:
        Path(args.out).write_text(md)
    if args.json:
        Path(args.json).write_text(report.as_json(diagnosis, toolbox.ledger))
    _summary(md)
    _gh_output(root_cause=diagnosis.root_cause_service, category=diagnosis.category)
    return 0


def cmd_scenario(args) -> int:
    from autopilot import scenarios
    from autopilot.doctor import llm
    if args.action == "list":
        for name in scenarios.names():
            sc = scenarios.Scenario.load(name)
            print(f"{name:30} {sc.mode:9} expect {sc.expect['root_cause_service']} / "
                  f"{sc.expect['category']}")
        return 0
    model = None if args.offline else llm.from_env()
    root = Path(args.evidence).resolve()
    names = [args.name] if args.action == "run" else (args.only or scenarios.names())
    outcomes = []
    for name in names:
        outcome = scenarios.run(name, root, model)
        outcomes.append(outcome)
        if args.action == "run":
            print(outcome.markdown)
        if args.examples:
            Path(args.examples).mkdir(parents=True, exist_ok=True)
            (Path(args.examples) / f"{name}.md").write_text(
                f"# Scenario: {name}\n\n> {outcome.scenario.description}\n\n{outcome.markdown}\n")
    board = scenarios.scoreboard(outcomes)
    print(board)
    _summary("### Root-cause agent evaluation\n\n" + board)
    return 0 if all(o.correct for o in outcomes) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="autopilot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("changes", help="changed and impacted services")
    s.add_argument("--base", required=True)
    s.add_argument("--head", default="HEAD")
    s.add_argument("--write", help="also write the change set to this file (e.g. ev/changes.json)")
    s.set_defaults(fn=cmd_changes)

    s = sub.add_parser("merge-check", help="decide whether a PR can merge automatically")
    s.add_argument("--pr", type=int)
    s.add_argument("--pr-json", help="PR facts as JSON instead of calling the GitHub API")
    s.add_argument("--checks", help='JSON of check results, e.g. {"unit": "success"}')
    s.add_argument("--required", nargs="*", default=[])
    s.set_defaults(fn=cmd_merge_check)

    s = sub.add_parser("stack", help="start or stop the local services")
    s.add_argument("action", choices=["up", "down"])
    s.add_argument("--evidence", default="evidence")
    s.add_argument("--fault", action="append", default=[], help="service=fault")
    s.add_argument("--withhold", action="append", default=[], help="service=ENV_KEY")
    s.set_defaults(fn=cmd_stack)

    s = sub.add_parser("test", help="run a test suite with retry and flaky detection")
    s.add_argument("suite")
    s.add_argument("paths", nargs="+")
    s.add_argument("--evidence", default="evidence")
    s.add_argument("--kind", choices=["unit", "integration"])
    s.add_argument("--service")
    s.add_argument("--no-retry", action="store_true")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("stage", help="record a pipeline stage result as evidence")
    s.add_argument("--evidence", default="evidence")
    s.add_argument("--name", required=True)
    s.add_argument("--kind", required=True, choices=["build", "unit", "integration", "deploy"])
    s.add_argument("--status", required=True, choices=["success", "failure", "skipped"])
    s.add_argument("--service")
    s.add_argument("--log", help="log path relative to the evidence directory")
    s.add_argument("--summary", default="")
    s.set_defaults(fn=cmd_stage)

    s = sub.add_parser("deploy", help="deploy with canary checks and automatic rollback")
    s.add_argument("target", choices=["local", "ecs"])
    s.add_argument("--version", required=True)
    s.add_argument("--evidence", default="evidence")
    s.add_argument("--service", action="append", help="local: services to deploy (default all)")
    s.add_argument("--environment", default="staging")
    s.add_argument("--fault", action="append",
                   help="local demo: ship a deliberately bad build, as service=fault")
    s.add_argument("--cluster")
    s.add_argument("--ecs-service")
    s.add_argument("--container")
    s.add_argument("--image")
    s.add_argument("--alarm", action="append", default=[])
    s.add_argument("--bake-seconds", type=int, default=300)
    s.set_defaults(fn=cmd_deploy)

    s = sub.add_parser("diagnose", help="find the root cause of a failed run")
    s.add_argument("--evidence", default="evidence")
    s.add_argument("--out")
    s.add_argument("--json")
    s.add_argument("--run-url")
    s.add_argument("--offline", action="store_true", help="ignore AUTOPILOT_LLM; use the playbook")
    s.set_defaults(fn=cmd_diagnose)

    s = sub.add_parser("scenario", help="fault scenarios and agent evaluation")
    s.add_argument("action", choices=["list", "run", "eval"])
    s.add_argument("name", nargs="?")
    s.add_argument("--only", nargs="*")
    s.add_argument("--evidence", default=".scenarios")
    s.add_argument("--examples", help="write each report to this directory")
    s.add_argument("--offline", action="store_true")
    s.set_defaults(fn=cmd_scenario)

    args = p.parse_args(argv)
    if args.command == "scenario" and args.action == "run" and not args.name:
        p.error("scenario run needs a scenario name")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
