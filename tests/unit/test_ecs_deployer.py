"""ECS deploy and rollback, against stubbed AWS APIs (botocore Stubber)."""

import boto3
import pytest
from botocore.stub import Stubber

from autopilot.deploy.base import Outcome
from autopilot.deploy.ecs import EcsDeployer, EcsTarget

OLD = "arn:aws:ecs:ap-south-1:123456789012:task-definition/payments:41"
NEW = "arn:aws:ecs:ap-south-1:123456789012:task-definition/payments:42"
TARGET = EcsTarget(cluster="prod", service="payments", container="app",
                   alarms=["payments-5xx"], bake_seconds=60, poll_seconds=30)
STABLE = {"services": [{"deployments": [{"status": "PRIMARY"}], "runningCount": 2,
                        "desiredCount": 2}], "failures": []}
MISSING = {"services": [], "failures": [{"arn": "x", "reason": "MISSING"}]}
DESCRIBE = {"cluster": "prod", "services": ["payments"]}


@pytest.fixture
def aws():
    ecs = boto3.client("ecs", region_name="ap-south-1", aws_access_key_id="x",
                       aws_secret_access_key="x")
    cw = boto3.client("cloudwatch", region_name="ap-south-1", aws_access_key_id="x",
                      aws_secret_access_key="x")
    with Stubber(ecs) as ecs_stub, Stubber(cw) as cw_stub:
        yield ecs, cw, ecs_stub, cw_stub
        ecs_stub.assert_no_pending_responses()
        cw_stub.assert_no_pending_responses()


def _start(ecs_stub):
    ecs_stub.add_response("describe_services", {"services": [{"taskDefinition": OLD}]},
                          {"cluster": "prod", "services": ["payments"]})
    ecs_stub.add_response("describe_task_definition", {"taskDefinition": {
        "family": "payments", "taskDefinitionArn": OLD, "revision": 41, "cpu": "256",
        "memory": "512", "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
        "containerDefinitions": [{"name": "app", "image": "repo/payments:old"},
                                 {"name": "sidecar", "image": "otel/collector:1"}]}},
        {"taskDefinition": OLD})
    ecs_stub.add_response("register_task_definition", {"taskDefinition": {"taskDefinitionArn": NEW}}, {
        "family": "payments", "cpu": "256", "memory": "512", "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "containerDefinitions": [{"name": "app", "image": "repo/payments:new"},
                                 {"name": "sidecar", "image": "otel/collector:1"}]})
    ecs_stub.add_response("update_service", {}, {
        "cluster": "prod", "service": "payments", "taskDefinition": NEW,
        "deploymentConfiguration": {"deploymentCircuitBreaker": {"enable": True, "rollback": True}}})


def _alarms(cw_stub, firing: list[str]):
    cw_stub.add_response("describe_alarms",
                         {"MetricAlarms": [{"AlarmName": a} for a in firing]},
                         {"AlarmNames": ["payments-5xx"], "StateValue": "ALARM"})


def deployer(ecs, cw):
    return EcsDeployer(ecs, cw, sleep=lambda s: None, waiter_delay=1, waiter_attempts=2)


def test_healthy_rollout_is_promoted(aws):
    ecs, cw, ecs_stub, cw_stub = aws
    _start(ecs_stub)
    ecs_stub.add_response("describe_services", STABLE, DESCRIBE)
    for _ in range(3):  # bake: t=0, 30, 60
        _alarms(cw_stub, [])
    result = deployer(ecs, cw).deploy(TARGET, "repo/payments:new", "v42")
    assert result.outcome == Outcome.PROMOTED
    assert result.previous_version == OLD


def test_alarm_during_bake_rolls_back(aws):
    ecs, cw, ecs_stub, cw_stub = aws
    _start(ecs_stub)
    ecs_stub.add_response("describe_services", STABLE, DESCRIBE)
    _alarms(cw_stub, [])
    _alarms(cw_stub, ["payments-5xx"])
    ecs_stub.add_response("update_service", {}, {"cluster": "prod", "service": "payments",
                                                 "taskDefinition": OLD})
    ecs_stub.add_response("describe_services", STABLE, DESCRIBE)
    result = deployer(ecs, cw).deploy(TARGET, "repo/payments:new", "v42")
    assert result.outcome == Outcome.ROLLED_BACK
    assert any("payments-5xx" in r for r in result.reasons)
    assert result.reasons[-1] == "rolled back to payments:41"


def test_failed_rollback_is_reported_as_failed(aws):
    ecs, cw, ecs_stub, cw_stub = aws
    _start(ecs_stub)
    ecs_stub.add_response("describe_services", MISSING, DESCRIBE)  # rollout fails
    ecs_stub.add_response("update_service", {}, {"cluster": "prod", "service": "payments",
                                                 "taskDefinition": OLD})  # rollback
    ecs_stub.add_response("describe_services", MISSING, DESCRIBE)  # ...and fails too
    result = deployer(ecs, cw).deploy(TARGET, "repo/payments:new", "v42")
    assert result.outcome == Outcome.FAILED
    assert any("rollback did not settle" in r for r in result.reasons)


def test_unknown_container_is_refused(aws):
    ecs, cw, ecs_stub, _ = aws
    ecs_stub.add_response("describe_services", {"services": [{"taskDefinition": OLD}]}, DESCRIBE)
    ecs_stub.add_response("describe_task_definition", {"taskDefinition": {
        "family": "payments", "containerDefinitions": [{"name": "web", "image": "x"}]}},
        {"taskDefinition": OLD})
    with pytest.raises(ValueError, match="container 'app'"):
        deployer(ecs, cw).deploy(TARGET, "repo/payments:new", "v42")
