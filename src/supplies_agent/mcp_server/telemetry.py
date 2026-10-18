"""OpenTelemetry for the MCP server: traces over OTLP.

The MCP SDK already wraps each request in a server span (`tools/call <tool>`) that joins the
agent's trace from the request's `_meta`. Each attempt to call the supplies API is a child span
(see client.py), so retries, injected faults and timeouts show up in the request's trace.

The caller travels in W3C baggage with the trace context; CallerSpanProcessor stamps it on every
span, as the agent service does. Export is on only when OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g.
in docker compose), so tests and plain local runs send nothing.
"""

import logging
import os

from opentelemetry import baggage, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

# The only baggage entries stamped onto spans: ids, never secrets or content.
BAGGAGE_KEYS = ("user.id", "session.id")


class CallerSpanProcessor(SpanProcessor):
    """Copies the caller's ids from baggage onto each span as it starts."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        for key in BAGGAGE_KEYS:
            value = baggage.get_baggage(key, parent_context)
            if value is not None:
                span.set_attribute(key, str(value))


def setup() -> None:
    """Export traces over OTLP/HTTP, if an endpoint is configured.

    Endpoint, service name and headers come from the standard OTEL_* env vars.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("telemetry export off: OTEL_EXPORTER_OTLP_ENDPOINT not set")
        return
    provider = TracerProvider()
    provider.add_span_processor(CallerSpanProcessor())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    logger.info("telemetry export on", extra={"endpoint": endpoint})


def log_trace() -> str:
    """For configure_logging: the current trace id on each log line, when there is one."""
    span = trace.get_current_span().get_span_context()
    return f" trace_id=<{span.trace_id:032x}>" if span.is_valid else ""
