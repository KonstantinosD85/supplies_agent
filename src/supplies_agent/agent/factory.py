"""Builds the Strands agent that serves one request."""

from typing import Any

from strands import Agent
from strands.models import Model

from supplies_agent.agent.prompts import SYSTEM_PROMPT
from supplies_agent.agent.schemas import RequestContext


def build_agent(model: Model, tools: list[Any], ctx: RequestContext) -> Agent:
    """A fresh agent per request, tagged with the caller's identity for traces."""
    return Agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,  # no console printing; the HTTP response carries the result
        trace_attributes={"user.id": ctx.user_id, "session.id": ctx.session_id},
    )
