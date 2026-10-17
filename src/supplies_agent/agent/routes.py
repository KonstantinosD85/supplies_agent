"""The HTTP endpoints. Wiring is in dependencies.py, refusals in errors.py, the agent run in
invocation.py."""

import functools
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, StreamingResponse

from supplies_agent.agent import invocation, streaming
from supplies_agent.agent.dependencies import PreparedInvocation, WantsStream
from supplies_agent.agent.schemas import ChatResponse

# The local web UI. On AgentCore the runtime only routes /invocations and /ping, so the page
# would be hosted elsewhere.
INDEX_HTML = Path(__file__).parent / "static" / "index.html"

router = APIRouter()


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "Healthy"}


@router.post(
    "/invocations",
    response_model=ChatResponse,
    responses={200: {"content": {streaming.MEDIA_TYPE: {}}}},
)
def invocations(
    prepared: PreparedInvocation, stream: WantsStream
) -> ChatResponse | StreamingResponse:
    """One invocation of the agent: a message, or decisions on the pending purchase orders.

    With `Accept: text/event-stream` the invocation streams as server-sent events (see
    schemas.STREAM_EVENTS); otherwise the response is the ChatResponse JSON.
    """
    if not stream:
        return invocation.run(prepared)
    # Started here, inside the request's context, so the invocation's spans join the request.
    events = streaming.start(functools.partial(invocation.run, prepared))
    return streaming.response(events, prepared.ctx)
