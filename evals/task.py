"""The eval task: runs the agent on one case, in isolation, and reports what happened.

The agent is built as the service builds it (real model, system prompt, approval hook and fact
store), but called directly: no HTTP, no session store. Only the supplies tools are fake (see
fake_supplies), so each case controls what they return.

A case's metadata says how to play it (all keys optional):

    facts:        {key: value} saved for the user before the case starts
    stock:        {sku: quantity} overrides on the seed stock
    unavailable:  tool names that fail as if the supplies system were down ("*" for all)
    then_say:     more user messages, sent one by one after the input
    decision:     "approve" or "decline", sent if the agent proposed a purchase order
"""

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from strands.models import Model
from strands_evals import Case
from strands_evals.extractors import tools_use_extractor
from strands_evals.types import EnvironmentState

from evals.fake_supplies import FakeSupplies
from supplies_agent.agent.approval import INTERRUPT_NAME
from supplies_agent.agent.factory import build_agent
from supplies_agent.agent.invocation import reply_text
from supplies_agent.agent.model import close_model
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER, RequestContext
from supplies_agent.memory import facts

USER = "alice"


def make_task(model_factory: Callable[[], Model]):
    """The task function for Experiment.run_evaluations_async, with a new model per case."""

    async def run_case(case: Case) -> dict[str, Any]:
        setup = case.metadata or {}
        supplies = FakeSupplies(
            stock=setup.get("stock", {}), unavailable=set(setup.get("unavailable", []))
        )
        ctx = RequestContext.model_validate({USER_HEADER: USER, SESSION_HEADER: case.name})
        model = model_factory()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "agent.db")
            facts.setup(db_path)
            store = facts.store(db_path, USER)
            for key, value in setup.get("facts", {}).items():
                await store.add(value, {"key": key})

            agent = build_agent(model, supplies.tools(), ctx, None, store)
            proposed = []
            try:
                result = await agent.invoke_async(case.input)
                for message in setup.get("then_say", []):
                    if result.stop_reason == "interrupt":
                        break  # an order is already waiting: the user can only decide now
                    result = await agent.invoke_async(message)
                interrupts = [i for i in result.interrupts or [] if i.name == INTERRUPT_NAME]
                proposed = [i.reason for i in interrupts]
                if interrupts and "decision" in setup:
                    # What the web UI's Approve / Decline buttons send.
                    decision = setup["decision"]
                    result = await agent.invoke_async(
                        [
                            {"interruptResponse": {"interruptId": i.id, "response": decision}}
                            for i in interrupts
                        ]
                    )
            finally:
                await close_model(model)

            saved = {e.metadata["key"]: e.content.split(": ", 1)[1] for e in await store.search("")}

        # Tool names in call order: what TrajectoryEvaluator's scorers compare with the case's
        # expected_trajectory. (Inputs and results would bloat the judge's prompt.)
        used = tools_use_extractor.extract_agent_tools_used_from_messages(agent.messages)
        trajectory = [t["name"] for t in used]
        state = {
            "tools_called": trajectory,
            "proposed_orders": proposed,
            "orders_placed": len(supplies.orders),
            "saved_facts": saved,
        }
        return {
            "output": reply_text(result),
            "trajectory": trajectory,
            "environment_state": [EnvironmentState(name=k, state=v) for k, v in state.items()],
        }

    return run_case
