"""The remember_fact tool: saves a lasting fact about the user through the memory manager."""

from typing import Annotated, Literal

from strands import ToolContext, tool
from strands.types.exceptions import AggregateMemoryError

from supplies_agent.memory import facts

# Same keys as facts.FACT_KEYS (a test keeps them in sync); spelled out for the tool schema.
FactKey = Literal["lab", "team", "delivery_location"]


@tool(context=True)
async def remember_fact(
    key: Annotated[FactKey, "Which fact: the user's lab, team, or default delivery_location."],
    value: Annotated[str, "The new value, e.g. 'Lab 3.12'. One short line."],
    tool_context: ToolContext,
) -> dict:
    """Remember a lasting fact about the user for future conversations.

    Use it when the user tells you their lab, their team, or where orders should normally be
    delivered. It replaces any previous value for that fact. Do not use it for a delivery
    location the user gives for one order only.
    """
    # The manager holds a store bound to the caller, so no user id passes through the model.
    try:
        await tool_context.agent.memory_manager.add(
            value, {"metadata": {"key": key}, "stores": [facts.STORE_NAME]}
        )
    except AggregateMemoryError as e:
        # The manager's message only names the store; the store's reason says what to fix.
        raise ValueError("; ".join(str(error) for error in e.errors)) from e
    return {"remembered": {key: value}}
