"""HTTP service: POST /invocations runs one agent turn for a user's session.

Follows the AgentCore Runtime HTTP contract (POST /invocations, GET /ping, port 8080),
so the same container runs locally and on AgentCore.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from strands import Agent
from strands.agent import AgentResult
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands.models import Model
from strands.types.agent import AgentInput
from strands.types.content import Messages
from strands.types.exceptions import MCPClientInitializationError, ModelThrottledException
from strands.types.tools import ToolResult

from supplies_agent.agent import approval, telemetry
from supplies_agent.agent.factory import build_agent
from supplies_agent.agent.model import build_model, close_model
from supplies_agent.agent.schemas import (
    ChatRequest,
    ChatResponse,
    RequestContext,
    ToolCall,
    Usage,
)
from supplies_agent.agent.session_locks import SessionBusy, SessionLocks
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.agent.tools import mcp_tools
from supplies_agent.logs import configure_logging
from supplies_agent.memory import facts, sessions
from supplies_agent.memory.sessions import SessionAccessDenied

logger = logging.getLogger(__name__)

ModelFactory = Callable[[AgentSettings], Model]
ToolSource = Callable[[AgentSettings, RequestContext], AbstractContextManager[list[Any]]]

MAX_RESULT_CHARS = 2000

# The local web UI. On AgentCore the runtime only routes /invocations and /ping, so the page
# would be hosted elsewhere.
INDEX_HTML = Path(__file__).parent / "static" / "index.html"


AWAITING_APPROVAL_REPLY = "Please approve or decline this purchase order."


class FinishedTools(HookProvider):
    """Results of the tools that finished in this turn, recorded as each one finishes.

    When the model calls several tools at once and one of them waits for approval, Strands
    runs the others but keeps their results out of agent.messages until the turn resumes.
    Recording them here lets the response show them as done instead of waiting.
    """

    def __init__(self) -> None:
        self.results: dict[str, ToolResult] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.record)

    def record(self, event: AfterToolCallEvent) -> None:
        self.results[event.tool_use["toolUseId"]] = event.result  # a retry overwrites


def extract_tool_calls(
    messages: Messages, start: int, finished: dict[str, ToolResult]
) -> list[ToolCall]:
    """Tool calls made or finished in this turn (messages[start:]), in call order.

    A call made in this turn may have no result yet: it waits for approval. A call can also
    start in an earlier turn: an approved order's toolUse was proposed in the previous turn and
    only runs now. Results finished in an earlier turn are not reported again.
    """
    uses = {
        block["toolUse"]["toolUseId"]: block["toolUse"]
        for message in messages
        for block in message["content"]
        if "toolUse" in block
    }
    turn_blocks = [block for message in messages[start:] for block in message["content"]]
    turn_uses = [block["toolUse"]["toolUseId"] for block in turn_blocks if "toolUse" in block]
    turn_results = {
        block["toolResult"]["toolUseId"]: block["toolResult"]
        for block in turn_blocks
        if "toolResult" in block
    }
    # Calls resumed from an earlier turn ran first, then the ones the model made in this turn.
    ids = [i for i in finished if i not in turn_uses] + turn_uses

    calls = []
    for tool_use_id in ids:
        use = uses.get(tool_use_id)
        if use is None:
            continue
        call = ToolCall(name=use["name"], input=use["input"])
        result = finished.get(tool_use_id) or turn_results.get(tool_use_id)
        if result is not None:
            call.status = result["status"]
            text = "\n".join(c["text"] for c in result["content"] if "text" in c)
            call.result = text[:MAX_RESULT_CHARS]
        calls.append(call)
    return calls


async def run_turn(agent: Agent, model: Model, prompt: AgentInput) -> AgentResult:
    """Run one turn and close the model's connections in the same event loop."""
    try:
        return await agent.invoke_async(prompt)
    finally:
        await close_model(model)


def reply_text(result: AgentResult) -> str:
    """The assistant's text. (str(result) would return the raw interrupts on an interrupt.)"""
    text = "\n".join(b["text"] for b in result.message["content"] if "text" in b).strip()
    if not text and result.stop_reason == "interrupt":
        return AWAITING_APPROVAL_REPLY
    return text


def caller(ctx: RequestContext) -> dict[str, str]:
    """Log fields naming the caller."""
    return {"user_id": ctx.user_id, "session_id": ctx.session_id}


def create_app(
    settings: AgentSettings | None = None,
    model_factory: ModelFactory = build_model,
    tool_source: ToolSource = mcp_tools,
) -> FastAPI:
    settings = settings or AgentSettings()
    app = FastAPI(title="Lab supplies agent", telemetry=telemetry.FASTAPI_TELEMETRY)
    locks = SessionLocks()

    sessions.setup(settings.db_path, settings.session_retention_days)
    facts.setup(settings.db_path)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(INDEX_HTML)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations")
    def invocations(
        request: ChatRequest,
        ctx: Annotated[RequestContext, Header()],
    ) -> ChatResponse:
        kind = "message" if request.message is not None else "approvals"
        with telemetry.caller_context(ctx, kind) as span:
            response = invoke(request, ctx)
            span.set_attributes(
                {
                    "supplies.stop_reason": response.stop_reason,
                    "supplies.pending_approvals": len(response.pending_approvals),
                }
            )
            return response

    def invoke(request: ChatRequest, ctx: RequestContext) -> ChatResponse:
        """One turn of the agent: the request's message, or decisions on pending orders."""
        started = time.perf_counter()
        try:
            with locks.hold(ctx.session_id):
                # Load the session first: it rejects someone else's session before any other work.
                session_manager = sessions.session_manager(
                    settings.db_path, ctx.user_id, ctx.session_id
                )
                with tool_source(settings, ctx) as tools:
                    model = model_factory(settings)
                    fact_store = facts.store(settings.db_path, ctx.user_id)
                    agent = build_agent(model, tools, ctx, session_manager, fact_store)
                    finished = FinishedTools()
                    agent.hooks.add_hook(finished)
                    pending = approval.pending_approvals(agent)
                    prompt = approval.agent_input(request, pending)
                    start = len(agent.messages)  # history restored from the session
                    result = asyncio.run(run_turn(agent, model, prompt))
                    pending = approval.record_pending(agent, result, session_manager)
        except SessionAccessDenied as e:
            logger.warning("session belongs to another user", extra=caller(ctx))
            raise HTTPException(
                status_code=403,
                detail="This session belongs to another user. Start a new session.",
            ) from e
        except approval.ApprovalConflict as e:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": e.message,
                    "pending_approvals": [p.model_dump() for p in e.pending],
                },
            ) from e
        except SessionBusy as e:
            raise HTTPException(
                status_code=409,
                detail="A previous message in this session is still being processed. "
                "Wait for it to finish, then retry.",
            ) from e
        except MCPClientInitializationError as e:
            logger.exception(
                "MCP server unreachable", extra={**caller(ctx), "mcp_url": settings.mcp_url}
            )
            raise HTTPException(
                status_code=503,
                detail="The supplies tools are unavailable right now. Please try again shortly.",
            ) from e
        except ModelThrottledException as e:
            logger.warning("model throttled", extra=caller(ctx))
            raise HTTPException(
                status_code=503,
                detail="The assistant is busy right now. Please try again in a moment.",
            ) from e

        usage = result.metrics.accumulated_usage
        response = ChatResponse(
            reply=reply_text(result),
            stop_reason=result.stop_reason,
            tool_calls=extract_tool_calls(agent.messages, start, finished.results),
            pending_approvals=pending,
            usage=Usage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
            ),
        )
        # Message text and tool results stay out of logs; traces carry them.
        logger.info(
            "invocation finished",
            extra={
                **caller(ctx),
                "stop_reason": response.stop_reason,
                "tools": ",".join(c.name for c in response.tool_calls),
                "tool_errors": sum(c.status == "error" for c in response.tool_calls),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "latency_ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return response

    return app


def main() -> None:
    configure_logging(trace=telemetry.log_trace)
    telemetry.setup()
    settings = AgentSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
