"""What a request needs, wired before the route runs.

Tests swap the model and the tools with app.dependency_overrides[get_model] /
app.dependency_overrides[get_tool_source].
"""

from collections.abc import AsyncIterator, Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from strands.memory import MemoryStore
from strands.models import Model

from supplies_agent.agent import invocation, streaming, telemetry
from supplies_agent.agent.model import build_model
from supplies_agent.agent.schemas import ChatRequest, RequestContext
from supplies_agent.agent.session_locks import SessionLocks
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.agent.tools import mcp_tools
from supplies_agent.memory import facts


def get_settings(request: Request) -> AgentSettings:
    return request.app.state.settings


def get_locks(request: Request) -> SessionLocks:
    return request.app.state.locks


Settings = Annotated[AgentSettings, Depends(get_settings)]
Locks = Annotated[SessionLocks, Depends(get_locks)]


async def get_caller(ctx: Annotated[RequestContext, Header()]) -> AsyncIterator[RequestContext]:
    """Who is calling, from the identity headers; tagged on the request span and in baggage.

    Async, so the baggage lands in the request's own context, which every later dependency
    and the route (each run in a worker thread) get a copy of.
    """
    with telemetry.caller_context(ctx):
        yield ctx


Caller = Annotated[RequestContext, Depends(get_caller)]


def wants_stream(accept: Annotated[str | None, Header()] = None) -> bool:
    """Whether the client asked for server-sent events (Accept: text/event-stream)."""
    return streaming.MEDIA_TYPE in (accept or "")


WantsStream = Annotated[bool, Depends(wants_stream)]


def get_model(settings: Settings) -> Model:
    return build_model(settings)


def get_tool_source() -> invocation.ToolSource:
    return mcp_tools


def get_fact_store(settings: Settings, ctx: Caller) -> MemoryStore:
    """Long-term memory, bound to the caller: no other user's facts are reachable."""
    return facts.store(settings.db_path, ctx.user_id)


def prepared_invocation(
    request: ChatRequest,  # the only dependency that reads the body
    ctx: Caller,
    settings: Settings,
    locks: Locks,
    model: Annotated[Model, Depends(get_model)],
    tool_source: Annotated[invocation.ToolSource, Depends(get_tool_source)],
    fact_store: Annotated[MemoryStore, Depends(get_fact_store)],
) -> Iterator[invocation.Invocation]:
    """The invocation, ready to run. A refusal (session busy or someone else's, tools
    unreachable, approval mismatch) is raised here, before any response starts; errors.py
    answers it.

    Leaving releases what invocation.run() didn't take over: everything, if it never ran.
    """
    telemetry.tag_request_kind("message" if request.message is not None else "approvals")
    with invocation.prepare(
        request,
        ctx,
        settings=settings,
        locks=locks,
        model=model,
        tool_source=tool_source,
        fact_store=fact_store,
    ) as prepared:
        yield prepared


PreparedInvocation = Annotated[invocation.Invocation, Depends(prepared_invocation)]
