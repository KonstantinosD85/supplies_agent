"""POST /invocations with Accept: text/event-stream: the invocation as server-sent events."""

import json

from scripted_model import ScriptedModel, tool_call
from strands import tool

from supplies_agent.agent.errors import UNEXPECTED_ERROR

STREAM = {"Accept": "text/event-stream"}
LINES = [{"sku": "LC-GLOVE-M", "quantity": 2}]


def events(response) -> list[tuple[str, dict]]:
    """The (event, data) pairs of an SSE response."""
    assert response.headers["content-type"].startswith("text/event-stream")
    pairs = []
    for block in response.text.strip().split("\n\n"):
        name, data = block.split("\n")
        pairs.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return pairs


def stock_model() -> ScriptedModel:
    return ScriptedModel(tool_call("check_stock", query="PBS"), "You have 6 packs of PBS.")


def test_a_turn_streams_tools_then_text_then_the_response(client_for):
    response = client_for(stock_model()).post(
        "/invocations", json={"message": "PBS?"}, headers=STREAM
    )
    assert response.status_code == 200

    streamed = events(response)
    assert [name for name, _ in streamed] == ["tool_start", "tool_end", "text", "done"]
    (_, start), (_, end), (_, text), (_, done) = streamed
    assert (start["name"], start["input"]) == ("check_stock", {"query": "PBS"})
    assert (end["id"], end["status"]) == (start["id"], "success")
    assert "BL-PBS-500" in end["result"]
    assert text == {"text": "You have 6 packs of PBS."}

    # "done" is the JSON path's response for the same invocation.
    plain = client_for(stock_model(), session_id="session-2").post(
        "/invocations", json={"message": "PBS?"}
    )
    assert done == plain.json()


def test_an_order_streams_its_proposal_and_its_approval(client_for):
    @tool
    def create_purchase_order(lines: list[dict], deliver_to: str) -> dict:
        """Raise a purchase order."""
        return {"po_number": "PO-000001"}

    model = ScriptedModel(
        tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"), "Placed."
    )
    client = client_for(model, tools=[create_purchase_order])

    proposed = events(client.post("/invocations", json={"message": "Gloves"}, headers=STREAM))
    assert [name for name, _ in proposed] == ["tool_start", "done"]  # waits: no tool_end
    pending = proposed[-1][1]["pending_approvals"]
    assert proposed[-1][1]["stop_reason"] == "interrupt"

    approvals = [{"interrupt_id": p["interrupt_id"], "decision": "approve"} for p in pending]
    approved = events(client.post("/invocations", json={"approvals": approvals}, headers=STREAM))
    assert [name for name, _ in approved] == ["tool_start", "tool_end", "text", "done"]
    assert approved[1][1]["status"] == "success"
    assert approved[-1][1]["pending_approvals"] == []


def test_a_refused_turn_keeps_its_http_status(client_for):
    client_for(ScriptedModel("Hi."), user_id="alice").post("/invocations", json={"message": "hi"})
    response = client_for(ScriptedModel(), user_id="bob").post(
        "/invocations", json={"message": "hi"}, headers=STREAM
    )
    assert response.status_code == 403


def test_a_failure_mid_turn_ends_the_stream_with_an_error(client_for):
    # The 200 went out with the first event, so the failure arrives as the last event.
    model = ScriptedModel(tool_call("check_stock", query="PBS"), RuntimeError("boom"), "Hi.")
    client = client_for(model)

    streamed = events(client.post("/invocations", json={"message": "PBS?"}, headers=STREAM))
    assert [name for name, _ in streamed] == ["tool_start", "tool_end", "error"]
    assert streamed[-1][1] == {"status": 500, "detail": UNEXPECTED_ERROR}

    # The session was released: the next invocation runs instead of getting 409.
    assert client.post("/invocations", json={"message": "again?"}).status_code == 200
