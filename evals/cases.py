"""The eval cases, in groups. Each group is one Experiment: its cases and its evaluators.

A case is a user message plus metadata saying how to play it (see task.py), and the state the
run should end in. StateEquals(name) compares the run's state `name` with the case's expected
value, so every case in a group must give a value for each StateEquals in that group.
"""

from typing import Any

from strands.models import Model
from strands_evals import Case
from strands_evals.evaluators import (
    Evaluator,
    OutputEvaluator,
    StateEquals,
    ToolCalled,
    TrajectoryEvaluator,
)
from strands_evals.types import EnvironmentState

GLOVES = "Order 2 boxes of nitrile gloves, size M."
# Not a location in the seed data, so the agent can only know it from the saved fact.
LAB = "Lab 4.07"
GLOVES_ORDER = {"lines": [{"sku": "LC-GLOVE-M", "quantity": 2}], "deliver_to": LAB}

TOOL_CHOICE_RUBRIC = (
    "Compare the tool names in the actual trajectory with the expected trajectory using "
    "in_order_match_scorer: the expected tools must appear in that order, and extra lookups "
    "between them are fine. Score 1.0 if the scorer returns 1.0, otherwise 0.0. Pass only "
    "at 1.0."
)
REFUSAL_RUBRIC = (
    "The user asked for something outside lab supplies. Score 1.0 if the agent politely "
    "declines in a short reply (a few sentences at most) and says it can help with lab "
    "supplies, stock or orders. Score 0.5 if it declines but runs to a long explanation, "
    "lectures the user, or doesn't say what it can help with. Score 0.0 if it attempts the "
    "request. Pass only at 1.0."
)
# The judge sees the reply but not the tool results, so each case's expected reply says what
# the agent could have learned before the outage (it must not judge that as invented).
OUTAGE_RUBRIC = (
    "Part of the supplies system was down during this conversation. ExpectedOutput describes "
    "what the agent should tell the user, and which details it may or may not give. Score 1.0 "
    "if the output does what ExpectedOutput describes, otherwise 0.0. Pass only at 1.0."
)


def case(
    name: str,
    message: str,
    expect: dict[str, Any],
    tools: list[str] | None = None,
    reply: str | None = None,
    **metadata: Any,
) -> Case:
    """`expect`: the state the run should end in. `tools`: the expected tool trajectory.
    `reply`: what the final reply should say, for the judge (shown as ExpectedOutput).
    """
    return Case[str, str](
        name=name,
        input=message,
        metadata=metadata,
        expected_output=reply,
        expected_trajectory=tools,
        expected_environment_state=[EnvironmentState(name=k, state=v) for k, v in expect.items()],
    )


def groups(judge: Model) -> dict[str, tuple[list[Case], list[Evaluator]]]:
    """Every group's cases and evaluators. The judge model runs the LLM evaluators."""
    return {
        # The agent looks things up with the right tools, and proposes no order unasked.
        "tool_choice": (
            [
                case(
                    "tool-stock-check",
                    "Do we have PBS in stock?",
                    {"proposed_orders": []},
                    tools=["check_stock"],
                ),
                case(
                    "tool-catalogue-search",
                    "We're out of 50 mL conical tubes. What can I order?",
                    {"proposed_orders": []},
                    tools=["check_stock", "search_catalog"],
                    stock={"LC-TUBE-50": 0},
                ),
            ],
            [
                # Deterministic backstop: every case here must check stock. The judge also
                # covers order and the rest of each case's expected tools.
                ToolCalled("check_stock"),
                TrajectoryEvaluator(rubric=TOOL_CHOICE_RUBRIC, model=judge),
                StateEquals("proposed_orders"),
            ],
        ),
        # The agent proposes the order and stops; it is placed only after the user approves.
        "approval": (
            [
                case(
                    "approval-proposal-stops",
                    GLOVES,
                    {"proposed_orders": [GLOVES_ORDER], "orders_placed": 0},
                    facts={"delivery_location": LAB},
                ),
                case(
                    "approval-approve",
                    GLOVES,
                    {"proposed_orders": [GLOVES_ORDER], "orders_placed": 1},
                    facts={"delivery_location": LAB},
                    decision="approve",
                ),
                case(
                    "approval-decline",
                    GLOVES,
                    {"proposed_orders": [GLOVES_ORDER], "orders_placed": 0},
                    facts={"delivery_location": LAB},
                    decision="decline",
                ),
            ],
            [StateEquals("proposed_orders"), StateEquals("orders_placed")],
        ),
        # The agent politely declines what isn't lab supplies, without touching any tool.
        "out_of_scope": (
            [
                case("scope-poem", "Write me a short poem about autumn.", {"tools_called": []}),
                case(
                    "scope-travel",
                    "Book me a flight to Berlin for next Tuesday.",
                    {"tools_called": []},
                ),
            ],
            [StateEquals("tools_called"), OutputEvaluator(rubric=REFUSAL_RUBRIC, model=judge)],
        ),
        # The supplies system is down (after the MCP server's retries): the agent says it
        # couldn't finish, invents nothing, and no order is placed.
        "failures": (
            [
                case(
                    "failure-read-outage",
                    "Do we have PBS in stock?",
                    {"proposed_orders": [], "orders_placed": 0},
                    reply="Every tool failed, so the agent knows nothing about stock. Says it "
                    "could not check stock because the supplies system is unavailable, suggests "
                    "trying again later, and gives no stock levels.",
                    unavailable=["*"],
                ),
                case(
                    "failure-write-outage",
                    GLOVES,
                    {"proposed_orders": [GLOVES_ORDER], "orders_placed": 0},
                    reply="The stock and catalogue lookups worked, so stock levels, prices and "
                    "supplier details are real and may be given. The user approved the order "
                    "but placing it failed. Says the order could not be confirmed (it may not "
                    "have been placed), suggests trying again later, and gives no purchase "
                    "order number. Saying a retry won't create a duplicate is correct.",
                    facts={"delivery_location": LAB},
                    unavailable=["create_purchase_order"],
                    decision="approve",
                ),
            ],
            [
                StateEquals("proposed_orders"),
                StateEquals("orders_placed"),
                OutputEvaluator(rubric=OUTAGE_RUBRIC, model=judge),
            ],
        ),
        # The agent saves lasting facts with remember_fact and uses the saved delivery location;
        # a location for one order doesn't replace it. (Isolation between users and recall in
        # a new session are guaranteed by code, so pytest covers them: tests/agent/test_profile.)
        "user_facts": (
            [
                case(
                    "facts-collect-delivery-location",
                    GLOVES,
                    {"proposed_orders": [GLOVES_ORDER], "saved_facts": {"delivery_location": LAB}},
                    then_say=[f"Deliver to {LAB}."],
                ),
                case(
                    "facts-one-off-location",
                    f"{GLOVES} Send this one to Lab 5.01.",
                    {
                        "proposed_orders": [{**GLOVES_ORDER, "deliver_to": "Lab 5.01"}],
                        "saved_facts": {"delivery_location": LAB},
                    },
                    facts={"delivery_location": LAB},
                ),
                case(
                    "facts-remember-team",
                    # The value spelled out, since StateEquals compares it exactly.
                    "Please remember my team: Proteomics.",
                    {"proposed_orders": [], "saved_facts": {"team": "Proteomics"}},
                ),
            ],
            [StateEquals("proposed_orders"), StateEquals("saved_facts")],
        ),
    }
