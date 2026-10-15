"""Request and response models of the agent service's HTTP API."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Who and which conversation travel in headers; the body carries only the turn.
# Set by AgentCore Runtime from the caller's runtimeSessionId; locally the client sets it.
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
# Same name the MCP server reads. On AgentCore it must be allowlisted on the runtime.
USER_HEADER = "X-User-Id"

# Ids end up in headers, keys and logs, so keep them to a safe alphabet.
ID_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


class ToolCall(BaseModel):
    name: str
    input: dict[str, Any]
    status: Literal["success", "error"] | None = None
    result: str | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ChatResponse(BaseModel):
    reply: str
    stop_reason: str
    tool_calls: list[ToolCall]
    usage: Usage


class RequestContext(BaseModel):
    """Who is calling, in which session. Read from headers, never from the body or the model."""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(alias=USER_HEADER, pattern=ID_PATTERN)
    session_id: str = Field(alias=SESSION_HEADER, pattern=ID_PATTERN)
