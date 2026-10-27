"""Agent evaluation: every fault scenario must be diagnosed correctly.

Each scenario really reproduces its failure (services with injected faults,
real integration tests, a real canary rollout, or a recorded build log) and
the agent must name the right service and category. Runs the offline
playbook by default; set AUTOPILOT_LLM / AUTOPILOT_LLM_MODEL to evaluate a
model instead.
"""

import pytest

from autopilot import scenarios
from autopilot.doctor import llm

pytestmark = pytest.mark.scenario


@pytest.mark.parametrize("name", scenarios.names())
def test_scenario_is_diagnosed_correctly(name, tmp_path):
    outcome = scenarios.run(name, tmp_path, llm.from_env())
    d = outcome.diagnosis
    assert (d.root_cause_service, d.category) == (
        outcome.scenario.expect["root_cause_service"], outcome.scenario.expect["category"]
    ), outcome.markdown
    assert d.evidence, "a diagnosis must cite evidence"
    assert d.actions, "a diagnosis must say what to do"
