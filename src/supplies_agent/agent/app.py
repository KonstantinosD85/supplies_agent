"""HTTP service: POST /invocations runs one agent turn for a user's session.

Follows the AgentCore Runtime HTTP contract (POST /invocations, GET /ping, port 8080),
so the same container runs locally and on AgentCore.
"""

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from strands.models import Model
from strands.types.content import Messages
from strands.types.exceptions import MCPClientInitializationError, ModelThrottledException

from supplies_agent.agent.factory import build_agent
from supplies_agent.agent.model import build_model
from supplies_agent.agent.schemas import (
    ChatRequest,
    ChatResponse,
    RequestContext,
    ToolCall,
    Usage,
)
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.agent.tools import mcp_tools
from supplies_agent.logs import configure_logging

logger = logging.getLogger(__name__)

ModelFactory = Callable[[AgentSettings], Model]
ToolSource = Callable[[AgentSettings, RequestContext], AbstractContextManager[list[Any]]]

MAX_RESULT_CHARS = 2000


def extract_tool_calls(messages: Messages) -> list[ToolCall]:
    """Pair each toolUse block with its toolResult, in call order."""
    calls: dict[str, ToolCall] = {}
    for message in messages:
        for block in message["content"]:
            if "toolUse" in block:
                use = block["toolUse"]
                calls[use["toolUseId"]] = ToolCall(name=use["name"], input=use["input"])
            elif "toolResult" in block:
                result = block["toolResult"]
                call = calls.get(result["toolUseId"])
                if call is not None:
                    call.status = result["status"]
                    text = "\n".join(c["text"] for c in result["content"] if "text" in c)
                    call.result = text[:MAX_RESULT_CHARS]
    return list(calls.values())


def create_app(
    settings: AgentSettings | None = None,
    model_factory: ModelFactory = build_model,
    tool_source: ToolSource = mcp_tools,
) -> FastAPI:
    settings = settings or AgentSettings()
    app = FastAPI(title="Lab supplies agent")

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
            with tool_source(settings, ctx) as tools:
                agent = build_agent(model_factory(settings), tools, ctx)
                start = len(agent.messages)
                result = agent(request.message)
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
            reply=str(result).strip(),
            stop_reason=result.stop_reason,
            tool_calls=extract_tool_calls(agent.messages[start:]),
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
