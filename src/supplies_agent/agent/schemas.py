"""Request and response models of the agent service's HTTP API."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Who and which conversation travel in headers; the body carries only the turn.
# Set by AgentCore Runtime from the caller's runtimeSessionId; locally the client sets it.
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
# Same name the MCP server reads. On AgentCore it must be allowlisted on the runtime.
USER_HEADER = "X-User-Id"

# Ids end up in headers, keys and logs, so keep them to a safe alphabet.
ID_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"


class ApprovalDecision(BaseModel):
    """The user's answer to one pending purchase order, from the UI's approve/decline buttons."""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=300)
    decision: Literal["approve", "decline"]


class ChatRequest(BaseModel):
    """One turn: either a new message, or decisions on the pending purchase orders."""

    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(None, min_length=1, max_length=4000)
    approvals: list[ApprovalDecision] | None = Field(None, min_length=1, max_length=10)

    @model_validator(mode="after")
    def one_kind_of_turn(self) -> Self:
        if (self.message is None) == (self.approvals is None):
            raise ValueError("send either 'message' or 'approvals', not both")
        return self


class ToolCall(BaseModel):
    name: str
    input: dict[str, Any]
    status: Literal["success", "error"] | None = None
    result: str | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class OrderLine(BaseModel):
    sku: str
    quantity: int


class PendingApproval(BaseModel):
    """A purchase order the agent proposed, waiting for the user's decision."""

    interrupt_id: str
    lines: list[OrderLine]
    deliver_to: str


class ChatResponse(BaseModel):
    reply: str
    stop_reason: str
    tool_calls: list[ToolCall]
    pending_approvals: list[PendingApproval] = []
    usage: Usage


class RequestContext(BaseModel):
    """Who is calling, in which session. Read from headers, never from the body or the model."""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(alias=USER_HEADER, pattern=ID_PATTERN)
    session_id: str = Field(alias=SESSION_HEADER, pattern=ID_PATTERN)
