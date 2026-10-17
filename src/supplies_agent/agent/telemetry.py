"""OpenTelemetry for the agent service: traces and metrics over OTLP, via StrandsTelemetry.

FastAPI creates the root span of each request (`POST /invocations`, with the HTTP status) and
HTTP metrics; Strands creates the agent, cycle, model and tool spans (with tokens, tool args and
results) and its metrics. We add the caller to the root span and put it in baggage, so every
span of the request carries user.id and session.id. The baggage also travels with
the MCP calls (W3C baggage header), ready for when the MCP server and the mock export traces.

Logs go over OTLP too (to Loki): each record keeps its `extra` fields as attributes and gets
the current trace and span ids, so a log line opens its trace and a trace lists its logs.

Export is on only when OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. in docker compose), so tests,
evals and plain local runs send nothing.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import baggage, context, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from strands.telemetry import StrandsTelemetry

from supplies_agent.agent.schemas import ChatResponse, RequestContext

logger = logging.getLogger(__name__)

# The only baggage entries stamped onto spans. Baggage is sent to every downstream service,
# so it carries ids only, never secrets or content.
BAGGAGE_KEYS = ("user.id", "session.id")


def baggage_key_allowed(key: str) -> bool:
    return key in BAGGAGE_KEYS


def setup() -> None:
    """Export traces, metrics and logs over OTLP/HTTP, if an endpoint is configured.

    Endpoint, service name and headers come from the standard OTEL_* env vars.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("telemetry export off: OTEL_EXPORTER_OTLP_ENDPOINT not set")
        return
    (
        StrandsTelemetry()
        .setup_baggage_processor(baggage_key_allowed)
        .setup_otlp_exporter()
        .setup_meter(enable_otlp_exporter=True)
    )
    # StrandsTelemetry covers traces and metrics only. Logs: everything that reaches the root
    # logger (our INFO and up, the SDK's WARNING and up) is exported as well as printed.
    logs = LoggerProvider()
    logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logs)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logs))
    logger.info("telemetry export on", extra={"endpoint": endpoint})


def log_trace() -> str:
    """For configure_logging: the current trace id on each log line, when there is one."""
    span = trace.get_current_span().get_span_context()
    return f" trace_id=<{span.trace_id:032x}>" if span.is_valid else ""


# FastAPI(telemetry=...): no spans for the healthcheck, and no per-operation spans
# (dependencies, endpoint, serialization), which only add noise under the request span.
FASTAPI_TELEMETRY = {
    "operation_spans": False,
    "exclude": lambda scope: scope.get("path") == "/ping",
}


@contextmanager
def caller_context(ctx: RequestContext) -> Iterator[None]:
    """Tag the request span with the caller, and put the caller in baggage for its children."""
    trace.get_current_span().set_attributes(  # FastAPI's request span
        {"user.id": ctx.user_id, "session.id": ctx.session_id}
    )
    caller = baggage.set_baggage("user.id", ctx.user_id)
    caller = baggage.set_baggage("session.id", ctx.session_id, context=caller)
    token = context.attach(caller)
    try:
        yield
    finally:
        context.detach(token)


def tag_request_kind(kind: str) -> None:
    """kind is "message" or "approvals"."""
    trace.get_current_span().set_attribute("supplies.request.kind", kind)


def record_outcome(response: ChatResponse) -> None:
    """The invocation's outcome on the request span (current here, and in a streaming worker)."""
    trace.get_current_span().set_attributes(
        {
            "supplies.stop_reason": response.stop_reason,
            "supplies.pending_approvals": len(response.pending_approvals),
        }
    )
