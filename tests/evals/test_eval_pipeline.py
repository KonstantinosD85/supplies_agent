"""Our eval code (case wiring and the task), run with a scripted model: no LLM calls.

The SDK's evaluators and the judges' rubrics are not tested here; real runs check those.
"""

import pytest
from scripted_model import ScriptedModel, tool_call
from strands_evals import Experiment
from strands_evals.evaluators import StateEquals

from evals.cases import GLOVES_ORDER, groups
from evals.task import make_task

pytestmark = pytest.mark.anyio

ORDER = tool_call("create_purchase_order", **GLOVES_ORDER)
GROUPS = groups(judge=ScriptedModel())
CASES = {c.name: (c, evaluators) for cases, evaluators in GROUPS.values() for c in cases}


def test_case_names_are_unique():
    names = [c.name for cases, _ in GROUPS.values() for c in cases]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("name", CASES)
def test_every_case_gives_a_value_for_each_state_its_group_checks(name):
    case, evaluators = CASES[name]
    expected = {s.name for s in case.expected_environment_state or []}
    checked = {e.name for e in evaluators if isinstance(e, StateEquals)}
    assert checked <= expected


async def run(name, *turns):
    """Run one case with a scripted agent model; returns the report and the run's state.

    Only for cases in groups without an LLM judge (the scripted judge has no answers).
    """
    case, evaluators = CASES[name]
    experiment = Experiment[str, str](cases=[case], evaluators=evaluators)
    report = await experiment.run_evaluations_async(
        make_task(lambda: ScriptedModel(*turns)), max_workers=1
    )
    state = {s["name"]: s["state"] for s in report.cases[0]["actual_environment_state"]}
    return report, state


async def test_an_approved_order_is_placed_and_passes():
    report, state = await run("approval-approve", ORDER, "Placed PO-000001.")
    assert report.test_passes == [True, True], report.reasons
    assert state["tools_called"] == ["create_purchase_order"]
    assert state["orders_placed"] == 1


async def test_a_declined_order_is_proposed_but_not_placed():
    report, state = await run("approval-decline", ORDER, "OK, I did not place it.")
    assert report.test_passes == [True, True], report.reasons
    assert state["proposed_orders"] == [GLOVES_ORDER]
    assert state["orders_placed"] == 0


async def test_no_proposal_fails_the_case_and_sends_no_decision():
    report, state = await run("approval-approve", "Where should I deliver the order?")
    assert report.test_passes == [False, False]
    assert state["proposed_orders"] == []
    assert state["tools_called"] == []


async def test_saved_facts_reach_the_agent_and_are_reported():
    model = ScriptedModel(ORDER)
    case, _ = CASES["approval-proposal-stops"]
    result = await make_task(lambda: model)(case)
    state = {s.name: s.state for s in result["environment_state"]}
    assert state["saved_facts"] == {"delivery_location": "Lab 4.07"}
    assert "Lab 4.07" in str(model.calls[0]["messages"])  # injected by the memory manager


async def test_a_failed_write_places_no_order():
    case, _ = CASES["failure-write-outage"]
    result = await make_task(lambda: ScriptedModel(ORDER, "I couldn't confirm the order."))(case)
    state = {s.name: s.state for s in result["environment_state"]}
    assert state["proposed_orders"] == [GLOVES_ORDER]
    assert state["orders_placed"] == 0


async def test_a_delivery_location_asked_for_is_saved_and_used():
    report, state = await run(
        "facts-collect-delivery-location",
        "Where should I deliver the order?",
        tool_call("remember_fact", key="delivery_location", value="Lab 4.07"),
        ORDER,
    )
    assert report.test_passes == [True, True], report.reasons
    assert state["tools_called"] == ["remember_fact", "create_purchase_order"]


async def test_an_order_proposed_before_the_user_answers_ends_the_case():
    report, state = await run("facts-collect-delivery-location", ORDER)  # no second message
    assert report.test_passes == [True, False]  # proposed, but nothing was saved
    assert state["saved_facts"] == {}
