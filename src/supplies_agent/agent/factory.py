"""Builds the Strands agent that serves one request."""

from typing import Any

from strands import Agent
from strands.models import Model
from strands.session import SessionManager

from supplies_agent.agent.prompts import SYSTEM_PROMPT
from supplies_agent.agent.schemas import RequestContext

AGENT_ID = "supplies-agent"


def build_agent(
    model: Model, tools: list[Any], ctx: RequestContext, session_manager: SessionManager
) -> Agent:
    """A fresh agent per request, restored from the session and tagged with the caller."""
    return Agent(
        agent_id=AGENT_ID,  # the session stores history per agent id
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,  # no console printing; the HTTP response carries the result
        trace_attributes={"user.id": ctx.user_id, "session.id": ctx.session_id},
        session_manager=session_manager,
    )
