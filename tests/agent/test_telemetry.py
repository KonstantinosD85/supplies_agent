"""Telemetry: one trace per /invocations request with the caller on every span, and the
approvals counter."""

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from opentelemetry.processor.baggage import BaggageSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from scripted_model import ScriptedModel, tool_call
from strands import tool

from supplies_agent.agent import telemetry
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER

EXPORTER = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
CALLER = {"user.id": "alice", "session.id": "session-1"}
_added = False


@pytest.fixture
def spans():
    """Spans finished during the test. The global provider can be set only once per process
    (other tests may have set it), so our processors, the ones telemetry.setup() adds, are added
    to whichever provider is there, once.
    """
    global _added
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    if not _added:
        provider = trace.get_tracer_provider()
        provider.add_span_processor(BaggageSpanProcessor(telemetry.baggage_key_allowed))
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        _added = True
    EXPORTER.clear()
    return EXPORTER


def by_name(exporter) -> dict:
    return {s.name: s for s in exporter.get_finished_spans()}


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_a_request_is_one_trace_with_the_caller_on_every_span(client_for, spans, accept):
    # Streamed, the turn runs in a worker thread: its spans must still join the request.
    model = ScriptedModel(tool_call("check_stock", query="PBS"), "6 bottles.")
    response = client_for(model).post(
        "/invocations", json={"message": "PBS?"}, headers={"Accept": accept}
    )
    assert response.status_code == 200

    finished = spans.get_finished_spans()
    root = by_name(spans)["POST /invocations"]  # FastAPI's request span
    assert root.parent is None
    assert root.kind == trace.SpanKind.SERVER
    assert {s.context.trace_id for s in finished} == {root.context.trace_id}
    assert any("check_stock" in s.name for s in finished)  # Strands' tool span, in the trace
    for span in finished:
        assert {k: span.attributes.get(k) for k in CALLER} == CALLER, span.name
    assert root.attributes["supplies.request.kind"] == "message"
    assert root.attributes["supplies.stop_reason"] == "end_turn"
    assert root.attributes["http.response.status_code"] == 200
    # Only the request span: no per-operation spans from FastAPI.
    assert not [s.name for s in finished if s.name.startswith("fastapi.")]


def test_the_healthcheck_is_not_traced(client_for, spans):
    assert client_for(ScriptedModel()).get("/ping").status_code == 200
    assert spans.get_finished_spans() == ()


def test_a_refused_request_is_traced_with_its_status_and_caller(client_for, spans):
    client_for(ScriptedModel("Hi."), user_id="alice").post("/invocations", json={"message": "hi"})
    spans.clear()
    response = client_for(ScriptedModel(), user_id="bob").post(
        "/invocations", json={"message": "hi"}
    )
    assert response.status_code == 403

    root = by_name(spans)["POST /invocations"]
    assert root.attributes["http.response.status_code"] == 403
    assert root.attributes["user.id"] == "bob"


def test_the_mcp_servers_span_joins_the_agents_trace_with_the_caller(
    app_for, mcp_agent_settings, spans
):
    model = ScriptedModel(tool_call("check_stock", query="PBS"), "In stock.")
    app = app_for(model, settings=mcp_agent_settings)
    headers = {USER_HEADER: "alice", SESSION_HEADER: "session-1"}
    response = TestClient(app).post("/invocations", json={"message": "PBS?"}, headers=headers)
    assert response.status_code == 200

    # The MCP server runs in this process, so its spans reach the same exporter.
    spans_ = by_name(spans)
    server_span = spans_["tools/call check_stock"]
    assert server_span.kind == trace.SpanKind.SERVER
    assert server_span.context.trace_id == spans_["POST /invocations"].context.trace_id
    assert {k: server_span.attributes.get(k) for k in CALLER} == CALLER  # from the baggage


@pytest.fixture
def approvals():
    """Read supplies.approvals by outcome (cumulative for the process, so tests use deltas)."""
    if not isinstance(metrics.get_meter_provider(), MeterProvider):
        metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))

    def read() -> dict[str, int]:
        data = METRICS.get_metrics_data()
        counts: dict[str, int] = {}
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == "supplies.approvals":
                        for point in metric.data.data_points:
                            counts[point.attributes["outcome"]] = point.value
        return counts

    return read


def test_approvals_are_counted_by_outcome(client_for, approvals):
    @tool
    def create_purchase_order(lines: list[dict], deliver_to: str) -> dict:
        """Raise a purchase order."""
        return {"po_number": "PO-000001"}

    order = tool_call("create_purchase_order", lines=[{"sku": "A", "quantity": 1}], deliver_to="L")
    before = approvals()
    for session, decision in [("s1", "approve"), ("s2", "decline")]:
        client = client_for(
            ScriptedModel(order, "Done."), session_id=session, tools=[create_purchase_order]
        )
        [pending] = client.post("/invocations", json={"message": "order"}).json()[
            "pending_approvals"
        ]
        approval = {"interrupt_id": pending["interrupt_id"], "decision": decision}
        client.post("/invocations", json={"approvals": [approval]})
    after = approvals()
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in ("proposed", "approved", "declined")}
    assert delta == {"proposed": 2, "approved": 1, "declined": 1}
