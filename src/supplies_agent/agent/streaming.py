"""Streaming an invocation as server-sent events.

The invocation runs in a worker thread with its own event loop (as the JSON path does), and
pushes its events onto a queue; the HTTP response reads them off as they arrive. The worker owns
the invocation to the end: if the client goes away mid-stream, it still finishes, records its
outcome and releases the session.
"""

import contextvars
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from supplies_agent.agent import errors
from supplies_agent.agent.schemas import STREAM_EVENTS, Emit, RequestContext, StreamError

logger = logging.getLogger(__name__)

MEDIA_TYPE = "text/event-stream"


def start(work: Callable[[Emit], BaseModel]) -> Iterator[BaseModel]:
    """Start work(emit) in a worker thread now; iterate what it emits, then what it returns.

    The worker runs in a copy of the caller's context (request span, baggage), taken here,
    so its spans and logs belong to the request. An exception in work is raised by the
    iterator, after the events emitted before it.
    """
    events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()

    def run() -> None:
        try:
            events.put(("done", work(lambda event: events.put(("event", event)))))
        except BaseException as e:  # handed to the reader, which re-raises it
            events.put(("failed", e))

    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(run,), daemon=True).start()
    return _read(events)


def _read(events: queue.SimpleQueue[tuple[str, Any]]) -> Iterator[BaseModel]:
    while True:
        kind, item = events.get()
        if kind == "failed":
            raise item
        yield item
        if kind == "done":
            return


def sse(event: BaseModel) -> str:
    """One server-sent event: the event's name, and its fields as JSON."""
    return f"event: {STREAM_EVENTS[type(event)]}\ndata: {event.model_dump_json()}\n\n"


def response(events: Iterator[BaseModel], ctx: RequestContext) -> StreamingResponse:
    """The events as an SSE response. A failure mid-invocation becomes a final "error" event: the
    status (200) went out with the first byte."""

    def encode() -> Iterator[str]:
        try:
            for event in events:
                yield sse(event)
        except errors.EXPECTED as e:
            status, detail = errors.answer(e, ctx.log_fields())
            yield sse(StreamError(status=status, detail=str(detail)))
        except Exception:
            logger.exception("streamed invocation failed", extra=ctx.log_fields())
            yield sse(StreamError(status=500, detail=errors.UNEXPECTED_ERROR))

    return StreamingResponse(encode(), media_type=MEDIA_TYPE)
