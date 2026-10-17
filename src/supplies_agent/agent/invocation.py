"""One invocation of the agent for a user's session: prepare it, run it, report it.

No HTTP here: the routes decide how an invocation is answered (JSON or a stream), the dependencies
gather what an invocation needs.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from strands import Agent
from strands.agent import AgentResult
from strands.memory import MemoryStore
from strands.models import Model
from strands.session import SessionManager
from strands.types.agent import AgentInput

from supplies_agent.agent import approval, telemetry
from supplies_agent.agent.factory import build_agent
from supplies_agent.agent.model import close_model
from supplies_agent.agent.schemas import (
    ChatRequest,
    ChatResponse,
    Emit,
    RequestContext,
    TextDelta,
    Usage,
)
from supplies_agent.agent.session_locks import SessionLocks
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.agent.tool_calls import ToolCalls
from supplies_agent.memory import sessions

logger = logging.getLogger(__name__)

ToolSource = Callable[[AgentSettings, RequestContext], AbstractContextManager[list[Any]]]

AWAITING_APPROVAL_REPLY = "Please approve or decline this purchase order."


def ignore(event: BaseModel) -> None:
    """An Emit for invocations nobody streams."""


@dataclass
class Invocation:
    """An invocation ready to run: the session held, the agent restored, its input checked."""

    ctx: RequestContext
    agent: Agent
    model: Model
    prompt: AgentInput
    session_manager: SessionManager
    history_length: int  # messages restored from the session, before this invocation
    started: float
    resources: ExitStack  # the session lock and the MCP connection; run() takes them over


@contextmanager
def prepare(
    request: ChatRequest,
    ctx: RequestContext,
    *,
    settings: AgentSettings,
    locks: SessionLocks,
    model: Model,
    tool_source: ToolSource,
    fact_store: MemoryStore,
) -> Iterator[Invocation]:
    """Everything that can refuse the invocation, before it runs (and before a stream starts).

    Raises SessionBusy, SessionAccessDenied, MCPClientInitializationError or ApprovalConflict.
    On leaving, releases whatever run() did not take over (nothing, once the invocation has run).
    """
    started = time.perf_counter()
    with ExitStack() as resources:
        resources.enter_context(locks.hold(ctx.session_id))
        # Load the session first: it rejects someone else's session before any other work.
        session_manager = sessions.session_manager(settings.db_path, ctx.user_id, ctx.session_id)
        tools = resources.enter_context(tool_source(settings, ctx))
        agent = build_agent(model, tools, ctx, session_manager, fact_store)
        prompt = approval.agent_input(request, approval.pending_approvals(agent))
        yield Invocation(
            ctx=ctx,
            agent=agent,
            model=model,
            prompt=prompt,
            session_manager=session_manager,
            history_length=len(agent.messages),
            started=started,
            resources=resources,
        )


def run(invocation: Invocation, emit: Emit = ignore) -> ChatResponse:
    """Run a prepared invocation, record its outcome, and release the session.

    Takes the invocation's resources over, so they are released when it ends even if nobody is
    waiting for the answer any more (a streaming client that went away).
    """
    with invocation.resources.pop_all():
        tool_calls = ToolCalls(emit)
        invocation.agent.hooks.add_hook(tool_calls)
        result = asyncio.run(
            stream_invocation(invocation.agent, invocation.model, invocation.prompt, emit)
        )
        pending = approval.record_pending(invocation.agent, result, invocation.session_manager)

    usage = result.metrics.accumulated_usage
    response = ChatResponse(
        reply=reply_text(result),
        stop_reason=result.stop_reason,
        tool_calls=tool_calls.report(invocation.agent.messages, invocation.history_length),
        pending_approvals=pending,
        usage=Usage(
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        ),
    )
    telemetry.record_outcome(response)
    # Message text and tool results stay out of logs; traces carry them.
    logger.info(
        "invocation finished",
        extra={
            **invocation.ctx.log_fields(),
            "stop_reason": response.stop_reason,
            "tools": ",".join(c.name for c in response.tool_calls),
            "tool_errors": sum(c.status == "error" for c in response.tool_calls),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "latency_ms": round((time.perf_counter() - invocation.started) * 1000),
        },
    )
    return response


async def stream_invocation(
    agent: Agent, model: Model, prompt: AgentInput, emit: Emit
) -> AgentResult:
    """Run one invocation, emitting the reply's text as the model writes it.

    Closes the model's connections in the same event loop.
    """
    try:
        async for event in agent.stream_async(prompt):
            if "data" in event:
                emit(TextDelta(text=event["data"]))
            elif "result" in event:
                result: AgentResult = event["result"]
        return result
    finally:
        await close_model(model)


def reply_text(result: AgentResult) -> str:
    """The assistant's text. (str(result) would return the raw interrupts on an interrupt.)"""
    text = "\n".join(b["text"] for b in result.message["content"] if "text" in b).strip()
    if not text and result.stop_reason == "interrupt":
        return AWAITING_APPROVAL_REPLY
    return text
