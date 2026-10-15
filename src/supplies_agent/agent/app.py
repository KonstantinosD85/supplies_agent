"""HTTP service: POST /invocations runs one agent turn for a user's session.

Follows the AgentCore Runtime HTTP contract (POST /invocations, GET /ping, port 8080),
so the same container runs locally and on AgentCore.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from strands import Agent
from strands.agent import AgentResult
from strands.models import Model
from strands.types.agent import AgentInput
from strands.types.content import Messages
from strands.types.exceptions import MCPClientInitializationError, ModelThrottledException

from supplies_agent.agent import approval
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
from supplies_agent.memory import sessions
from supplies_agent.memory.sessions import SessionAccessDenied

logger = logging.getLogger(__name__)

ModelFactory = Callable[[AgentSettings], Model]
ToolSource = Callable[[AgentSettings, RequestContext], AbstractContextManager[list[Any]]]

MAX_RESULT_CHARS = 2000


AWAITING_APPROVAL_REPLY = "Please approve or decline this purchase order."


def extract_tool_calls(messages: Messages, start: int) -> list[ToolCall]:
    """Tool calls made or finished in this turn (messages[start:]), in call order.

    A call can start in an earlier turn: an approved order's toolUse was proposed in the
    previous turn and only gets its result now.
    """
    uses = {
        block["toolUse"]["toolUseId"]: block["toolUse"]
        for message in messages
        for block in message["content"]
        if "toolUse" in block
    }
    calls: dict[str, ToolCall] = {}
    for message in messages[start:]:
        for block in message["content"]:
            if "toolUse" in block:
                use = block["toolUse"]
                calls[use["toolUseId"]] = ToolCall(name=use["name"], input=use["input"])
            elif "toolResult" in block:
                result = block["toolResult"]
                use = uses.get(result["toolUseId"])
                if use is None:
                    continue
                call = calls.setdefault(
                    result["toolUseId"], ToolCall(name=use["name"], input=use["input"])
                )
                call.status = result["status"]
                text = "\n".join(c["text"] for c in result["content"] if "text" in c)
                call.result = text[:MAX_RESULT_CHARS]
    return list(calls.values())


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


def create_app(
    settings: AgentSettings | None = None,
    model_factory: ModelFactory = build_model,
    tool_source: ToolSource = mcp_tools,
) -> FastAPI:
    settings = settings or AgentSettings()
    app = FastAPI(title="Lab supplies agent")
    locks = SessionLocks()

    sessions.setup(settings.db_path, settings.session_retention_days)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations")
    def invocations(
        request: ChatRequest,
        ctx: Annotated[RequestContext, Header()],
    ) -> ChatResponse:
        started = time.perf_counter()
        try:
            with locks.hold(ctx.session_id):
                # Load the session first: it rejects someone else's session before any other work.
                session_manager = sessions.session_manager(
                    settings.db_path, ctx.user_id, ctx.session_id
                )
                with tool_source(settings, ctx) as tools:
                    model = model_factory(settings)
                    agent = build_agent(model, tools, ctx, session_manager)
                    pending = approval.pending_approvals(agent)
                    prompt = approval.agent_input(request, pending)
                    start = len(agent.messages)  # history restored from the session
                    result = asyncio.run(run_turn(agent, model, prompt))
                    pending = approval.record_pending(agent, result, session_manager)
        except SessionAccessDenied as e:
            logger.warning(
                "user_id=<%s>, session_id=<%s> | session belongs to another user",
                ctx.user_id,
                ctx.session_id,
            )
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
                "user_id=<%s>, session_id=<%s>, mcp_url=<%s> | MCP server unreachable",
                ctx.user_id,
                ctx.session_id,
                settings.mcp_url,
            )
            raise HTTPException(
                status_code=503,
                detail="The supplies tools are unavailable right now. Please try again shortly.",
            ) from e
        except ModelThrottledException as e:
            logger.warning(
                "user_id=<%s>, session_id=<%s> | model throttled", ctx.user_id, ctx.session_id
            )
            raise HTTPException(
                status_code=503,
                detail="The assistant is busy right now. Please try again in a moment.",
            ) from e

        usage = result.metrics.accumulated_usage
        response = ChatResponse(
            reply=reply_text(result),
            stop_reason=result.stop_reason,
            tool_calls=extract_tool_calls(agent.messages, start),
            pending_approvals=pending,
            usage=Usage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
            ),
        )
        # Message text and tool results stay out of logs; traces carry them.
        logger.info(
            "user_id=<%s>, session_id=<%s>, stop_reason=<%s>, tools=<%s>, tool_errors=<%d>, "
            "input_tokens=<%d>, output_tokens=<%d>, latency_ms=<%d> | invocation finished",
            ctx.user_id,
            ctx.session_id,
            response.stop_reason,
            ",".join(c.name for c in response.tool_calls),
            sum(c.status == "error" for c in response.tool_calls),
            response.usage.input_tokens,
            response.usage.output_tokens,
            (time.perf_counter() - started) * 1000,
        )
        return response

    return app


def main() -> None:
    configure_logging()
    settings = AgentSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
