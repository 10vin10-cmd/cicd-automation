"""Deploy a new image to an Amazon ECS service, watch it, roll back on trouble.

1. Register a new task definition revision with the new image.
2. Update the service (with ECS's deployment circuit breaker on) and wait for
   it to reach a steady state.
3. Bake: watch the service's CloudWatch alarms for a fixed period.
4. If the rollout fails or any alarm fires, point the service back at the
   previous task definition and wait for that to settle.

AWS clients and the sleep function are injected, so the whole flow is tested
offline with botocore's Stubber.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from botocore.exceptions import ClientError, WaiterError

from autopilot.deploy.base import DeployResult, Outcome

# Task definition fields that register_task_definition accepts back.
_REGISTRABLE = ("family", "taskRoleArn", "executionRoleArn", "networkMode",
                "containerDefinitions", "volumes", "placementConstraints",
                "requiresCompatibilities", "cpu", "memory", "pidMode", "ipcMode",
                "proxyConfiguration", "runtimePlatform", "ephemeralStorage")


@dataclass
class EcsTarget:
    cluster: str
    service: str
    container: str
    alarms: list[str] = field(default_factory=list)
    bake_seconds: int = 300
    poll_seconds: int = 30


class EcsDeployer:
    name = "ecs"

    def __init__(self, ecs, cloudwatch, sleep: Callable[[float], None] = time.sleep,
                 waiter_delay: int = 15, waiter_attempts: int = 40):
        self.ecs = ecs
        self.cloudwatch = cloudwatch
        self.sleep = sleep
        self.waiter_config = {"Delay": waiter_delay, "MaxAttempts": waiter_attempts}

    def deploy(self, target: EcsTarget, image: str, version: str) -> DeployResult:
        current = self.ecs.describe_services(cluster=target.cluster, services=[target.service])
        previous_td = current["services"][0]["taskDefinition"]
        result = DeployResult(service=target.service, version=version,
                              previous_version=previous_td, outcome=Outcome.PROMOTED,
                              target=self.name)

        new_td = self._register_with_image(previous_td, target.container, image)
        result.reasons.append(f"registered {new_td.rsplit('/', 1)[-1]}")
        self.ecs.update_service(
            cluster=target.cluster, service=target.service, taskDefinition=new_td,
            deploymentConfiguration={
                "deploymentCircuitBreaker": {"enable": True, "rollback": True}},
        )

        problem = self._wait_stable(target) or self._bake(target)
        if problem is None:
            result.reasons.append(f"stable and healthy for {target.bake_seconds}s")
            return result

        result.reasons.append(problem)
        try:
            self.ecs.update_service(cluster=target.cluster, service=target.service,
                                    taskDefinition=previous_td)
            rollback_problem = self._wait_stable(target)
        except ClientError as err:
            rollback_problem = f"rollback call failed: {err}"
        if rollback_problem:
            result.outcome = Outcome.FAILED
            result.reasons.append(f"rollback did not settle: {rollback_problem}")
        else:
            result.outcome = Outcome.ROLLED_BACK
            result.reasons.append(f"rolled back to {previous_td.rsplit('/', 1)[-1]}")
        return result

    def _register_with_image(self, task_def_arn: str, container: str, image: str) -> str:
        td = self.ecs.describe_task_definition(taskDefinition=task_def_arn)["taskDefinition"]
        containers = td["containerDefinitions"]
        if not any(c["name"] == container for c in containers):
            raise ValueError(f"container {container!r} not in {task_def_arn}")
        new = {k: td[k] for k in _REGISTRABLE if k in td}
        new["containerDefinitions"] = [
            {**c, "image": image} if c["name"] == container else c for c in containers]
        return self.ecs.register_task_definition(**new)["taskDefinition"]["taskDefinitionArn"]

    def _wait_stable(self, target: EcsTarget) -> str | None:
        try:
            self.ecs.get_waiter("services_stable").wait(
                cluster=target.cluster, services=[target.service],
                WaiterConfig=self.waiter_config)
        except WaiterError as err:
            return f"service did not reach a steady state: {err.last_response.get('failures') or err}"
        return None

    def _bake(self, target: EcsTarget) -> str | None:
        if not target.alarms:
            return None
        waited = 0
        while True:
            firing = [a["AlarmName"] for a in self.cloudwatch.describe_alarms(
                AlarmNames=target.alarms, StateValue="ALARM")["MetricAlarms"]]
            if firing:
                return f"alarm(s) firing during bake: {', '.join(sorted(firing))}"
            if waited >= target.bake_seconds:
                return None
            self.sleep(target.poll_seconds)
            waited += target.poll_seconds
