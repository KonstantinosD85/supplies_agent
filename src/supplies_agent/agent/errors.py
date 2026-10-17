"""What the user is told when an invocation can't run or can't finish.

Registered as exception handlers, so the routes and dependencies just raise. All but
ModelThrottledException are raised while preparing the invocation, so a streamed request still gets
them as an HTTP status; a failure mid-stream becomes the stream's "error" event instead.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from strands.types.exceptions import MCPClientInitializationError, ModelThrottledException

from supplies_agent.agent.approval import ApprovalConflict
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER
from supplies_agent.agent.session_locks import SessionBusy
from supplies_agent.memory.sessions import SessionAccessDenied

logger = logging.getLogger(__name__)

EXPECTED = (
    SessionAccessDenied,
    ApprovalConflict,
    SessionBusy,
    MCPClientInitializationError,
    ModelThrottledException,
)

UNEXPECTED_ERROR = "Something went wrong on our side. Please try again."


def answer(e: Exception, log_fields: dict[str, Any]) -> tuple[int, Any]:
    """The status and detail for one of EXPECTED, logged where it needs a trail."""
    match e:
        case SessionAccessDenied():
            logger.warning("session belongs to another user", extra=log_fields)
            return 403, "This session belongs to another user. Start a new session."
        case ApprovalConflict():
            return 409, {
                "message": e.message,
                "pending_approvals": [p.model_dump() for p in e.pending],
            }
        case SessionBusy():
            return 409, (
                "A previous message in this session is still being processed. "
                "Wait for it to finish, then retry."
            )
        case MCPClientInitializationError():  # logged by tools.mcp_tools, with the URL
            return 503, "The supplies tools are unavailable right now. Please try again shortly."
        case ModelThrottledException():
            logger.warning("model throttled", extra=log_fields)
            return 503, "The assistant is busy right now. Please try again in a moment."
    raise TypeError(f"not an expected error: {e!r}")


def register(app: FastAPI) -> None:
    async def handle(request: Request, e: Exception) -> JSONResponse:
        # The headers were validated by the time anything here is raised.
        log_fields = {
            "user_id": request.headers.get(USER_HEADER),
            "session_id": request.headers.get(SESSION_HEADER),
        }
        status, detail = answer(e, log_fields)
        return JSONResponse({"detail": detail}, status_code=status)

    for error in EXPECTED:
        app.add_exception_handler(error, handle)
