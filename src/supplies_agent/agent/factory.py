"""Builds the Strands agent that serves one request."""

from typing import Any

from strands import Agent
from strands.memory import MemoryManager, MemoryStore
from strands.models import Model
from strands.session import SessionManager

from supplies_agent.agent.approval import ApprovalHook
from supplies_agent.agent.profile import remember_fact
from supplies_agent.agent.prompts import SYSTEM_PROMPT
from supplies_agent.agent.schemas import RequestContext

AGENT_ID = "supplies-agent"


def build_memory_manager(fact_store: MemoryStore) -> MemoryManager:
    """Long-term memory: the user's facts are injected on each new message.

    No search_memory tool (the whole profile is injected anyway) and no free-text add_memory
    tool: remember_fact is the only way to write, one value per fact.
    """
    return MemoryManager(
        stores=[fact_store],
        search_tool_config=False,
        add_tool_config=False,
        injection={
            # The profile is a few rows, so fetch all of it whatever the user wrote.
            "query": lambda _: "user profile",
            # Every model call, not only the first of a turn: the model picks the delivery
            # location after its lookups, when the latest message is a tool result.
            "trigger": "everyTurn",
        },
    )


def build_agent(
    model: Model,
    tools: list[Any],
    ctx: RequestContext,
    session_manager: SessionManager,
    fact_store: MemoryStore,
) -> Agent:
    """A fresh agent per request, restored from the session and tagged with the caller."""
    return Agent(
        agent_id=AGENT_ID,  # the session stores history per agent id
        model=model,
        tools=[*tools, remember_fact],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,  # no console printing; the HTTP response carries the result
        trace_attributes={"user.id": ctx.user_id, "session.id": ctx.session_id},
        session_manager=session_manager,
        memory_manager=build_memory_manager(fact_store),  # bound to this user's facts only
        hooks=[ApprovalHook()],  # create_purchase_order only runs after the user approves
    )
