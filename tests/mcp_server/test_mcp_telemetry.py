"""Telemetry: each attempt to call the supplies API is a span, with the caller from baggage."""

import httpx2
import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from supplies_agent.mcp_server.client import SuppliesApi
from supplies_agent.mcp_server.telemetry import CallerSpanProcessor

pytestmark = pytest.mark.anyio

EXPORTER = InMemorySpanExporter()
_added = False


@pytest.fixture
def spans():
    """The global provider can be set only once per process (other tests may have set it), so
    our processors are added to whichever provider is there, once."""
    global _added
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    if not _added:
        provider = trace.get_tracer_provider()
        provider.add_span_processor(CallerSpanProcessor())
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        _added = True
    EXPORTER.clear()
    return EXPORTER


async def test_each_attempt_is_a_span_with_the_fault_and_the_caller(mcp_settings, spans):
    responses = [
        httpx2.Response(503, headers={"X-Fault-Injected": "server_error"}),
        httpx2.Response(200, json=[]),
    ]
    transport = httpx2.MockTransport(lambda request: responses.pop(0))
    caller = baggage.set_baggage("user.id", "alice")
    token = context.attach(baggage.set_baggage("session.id", "s1", context=caller))
    try:
        await SuppliesApi(mcp_settings, transport).request("GET", "/stock")
    finally:
        context.detach(token)

    failed, ok = sorted(
        (s for s in spans.get_finished_spans() if s.name == "supplies API GET"),
        key=lambda s: s.attributes["supplies.attempt"],
    )
    assert failed.attributes["http.response.status_code"] == 503
    assert failed.attributes["supplies.fault_injected"] == "server_error"
    assert failed.status.status_code == StatusCode.ERROR
    assert ok.attributes["http.response.status_code"] == 200
    assert ok.status.status_code != StatusCode.ERROR
    for span in (failed, ok):
        assert span.attributes["user.id"] == "alice"
        assert span.attributes["session.id"] == "s1"
