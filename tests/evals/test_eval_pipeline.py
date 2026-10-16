"""The eval pipeline itself (cases, harness, evaluators), run with scripted models: no LLM calls."""

import inspect
from contextlib import contextmanager

import pytest
from scripted_model import ScriptedModel, tool_call
from strands_evals import Experiment
from strands_evals.types import EnvironmentState

from evals import cases as eval_cases
from evals.evaluators import CHECKS, RUN_STATE, RubricJudge, ScenarioChecks
from evals.harness import NOTHING_TO_DECIDE, play, transcript
from supplies_agent.agent.settings import AgentSettings

ORDER = tool_call(
    "create_purchase_order", lines=[{"sku": "LC-GLOVE-M", "quantity": 2}], deliver_to=eval_cases.LAB
)


@pytest.mark.parametrize("case", eval_cases.CASES, ids=lambda c: c.name)
def test_every_check_exists_and_takes_its_arguments(case):
    for spec in case.metadata["checks"]:
        check = CHECKS[spec["check"]]
        args = {k: v for k, v in spec.items() if k != "check"}
        inspect.signature(check).bind({}, **args)  # raises on unknown or missing arguments


def test_case_names_are_unique():
    names = [c.name for c in eval_cases.CASES]
    assert len(names) == len(set(names))


@contextmanager
def scripted(*turns):
    """Run the experiment's cases with one scripted agent model and a scripted judge."""
    agent_model = ScriptedModel(*turns)

    def task(case):
        run = play(case.input, case.metadata["setup"], AgentSettings(), lambda _: agent_model)
        return {
            "output": transcript(run),
            "trajectory": run["trajectory"],
            "environment_state": [EnvironmentState(name=RUN_STATE, state=run)],
        }

    yield task


def run_case(name, task, judge=None):
    [case] = [c for c in eval_cases.CASES if c.name == name]
    evaluators = [ScenarioChecks(), RubricJudge(judge or (lambda: ScriptedModel()))]
    return Experiment[list, str](cases=[case], evaluators=evaluators).run_evaluations(task)


def test_a_passing_case_passes_every_evaluator():
    with scripted(ORDER, "Placed PO-000001.") as task:
        report = run_case("approval-approve", task)
    assert report.test_passes == [True, True], report.reasons
    # No rubric on this case, so the judge reports "not applicable" instead of a verdict.
    assert report.reasons[1] == "no rubric"


def test_a_failing_check_fails_the_case_with_its_reason():
    with scripted(ORDER, "Declined.") as task:
        report = run_case("approval-approve", task)  # scripted "approve" never reaches a PO
    checks_pass, _ = report.test_passes
    assert not checks_pass
    assert "FAIL reply_includes_any" in report.reasons[0]


def test_a_decision_with_nothing_waiting_is_not_sent_and_fails_the_case():
    with scripted("Where should I deliver the order?") as task:
        report = run_case("approval-approve", task)
    assert not report.test_passes[0]
    assert (
        f"FAIL all_requests_succeeded: requests failed or not sent: ['{NOTHING_TO_DECIDE}']"
        in (report.reasons[0])
    )


def test_the_judge_scores_cases_with_a_rubric():
    verdict = tool_call("EvaluationOutput", score=1.0, test_pass=True, reason="Polite refusal.")
    with scripted("I can only help with lab supplies, stock and orders.") as task:
        report = run_case("scope-poem", task, judge=lambda: ScriptedModel(verdict))
    assert report.test_passes == [True, True], report.reasons
    assert report.reasons[1] == "Polite refusal."
