"""The approval gate through /invocations: propose, then approve or decline in the next one."""

import pytest
from scripted_model import ScriptedModel, tool_call
from strands import tool

from supplies_agent.agent.approval import DECLINED_MESSAGE, MISSING_DELIVERY_MESSAGE
from supplies_agent.agent.invocation import AWAITING_APPROVAL_REPLY

LINES = [{"sku": "LC-GLOVE-M", "quantity": 2}]


@pytest.fixture
def placed() -> list:
    return []


@pytest.fixture
def order_tool(placed):
    @tool
    def create_purchase_order(lines: list[dict], deliver_to: str | None = None) -> dict:
        """Raise a purchase order."""
        placed.append(lines)
        return {"po_number": f"PO-00000{len(placed)}"}

    return create_purchase_order


@pytest.fixture
def client(client_for, order_tool):
    """Each call builds a new app on the same database, like a restart between invocations."""
    return lambda model: client_for(model, tools=[order_tool])


def propose(client, model) -> dict:
    response = client(model).post("/invocations", json={"message": "Order 2 boxes of gloves"})
    assert response.status_code == 200, response.text
    return response.json()


def decide(client, model, body: dict, decision: str):
    approvals = [
        {"interrupt_id": p["interrupt_id"], "decision": decision} for p in body["pending_approvals"]
    ]
    return client(model).post("/invocations", json={"approvals": approvals})


def test_proposal_stops_the_turn_before_the_order_is_placed(client, placed):
    model = ScriptedModel(tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"))
    body = propose(client, model)

    assert body["stop_reason"] == "interrupt"
    assert body["reply"] == AWAITING_APPROVAL_REPLY
    [pending] = body["pending_approvals"]
    assert pending["lines"] == LINES
    assert placed == []  # nothing written yet
    [call] = body["tool_calls"]
    assert (call["name"], call["status"]) == ("create_purchase_order", None)


def test_approve_places_the_order_once(client, placed):
    model = ScriptedModel(
        tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"), "Placed PO-000001."
    )
    body = propose(client, model)

    response = decide(client, model, body, "approve")  # a new app: survives a restart
    assert response.status_code == 200, response.text
    approved = response.json()

    assert placed == [LINES]
    assert approved["reply"] == "Placed PO-000001."
    assert approved["pending_approvals"] == []
    [call] = approved["tool_calls"]
    assert (call["name"], call["status"]) == ("create_purchase_order", "success")
    assert "PO-000001" in call["result"]


def test_decline_does_not_place_the_order(client, placed):
    model = ScriptedModel(
        tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"), "OK, not placed."
    )
    body = propose(client, model)

    declined = decide(client, model, body, "decline").json()

    assert placed == []
    assert declined["reply"] == "OK, not placed."
    [call] = declined["tool_calls"]
    assert call["status"] == "error"
    assert DECLINED_MESSAGE in str(model.calls[-1]["messages"][-1])  # the model is told why


def test_every_order_needs_its_own_approval(client, placed):
    model = ScriptedModel(
        tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"),
        "Placed.",
        tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"),
    )
    decide(client, model, propose(client, model), "approve")

    again = client(model).post("/invocations", json={"message": "Same again please"}).json()
    assert again["stop_reason"] == "interrupt"  # no standing approval
    assert len(placed) == 1


def test_message_while_an_order_waits_is_refused(client, placed):
    model = ScriptedModel(tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"))
    body = propose(client, model)

    response = client(model).post("/invocations", json={"message": "yes"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Approve or decline" in detail["message"]
    assert detail["pending_approvals"] == body["pending_approvals"]  # so the UI can recover
    assert placed == []
    assert len(model.calls) == 1  # "yes" never reaches the model


def test_decision_without_a_pending_order_is_refused(client):
    response = client(ScriptedModel()).post(
        "/invocations", json={"approvals": [{"interrupt_id": "x", "decision": "approve"}]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["pending_approvals"] == []


def test_decision_for_an_unknown_order_is_refused(client, placed):
    model = ScriptedModel(tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"))
    propose(client, model)

    response = client(model).post(
        "/invocations", json={"approvals": [{"interrupt_id": "made-up", "decision": "approve"}]}
    )
    assert response.status_code == 409
    assert placed == []


@pytest.mark.parametrize(
    "body",
    [
        {"message": "hi", "approvals": [{"interrupt_id": "x", "decision": "approve"}]},
        {"approvals": [{"interrupt_id": "x", "decision": "yes"}]},  # only approve/decline
        {"approvals": []},
    ],
)
def test_malformed_decisions_are_rejected(client, body):
    assert client(ScriptedModel()).post("/invocations", json=body).status_code == 422


def test_order_without_a_delivery_location_is_sent_back_not_proposed(client, placed):
    model = ScriptedModel(tool_call("create_purchase_order", lines=LINES), "Where to?")
    body = propose(client, model)

    assert body["pending_approvals"] == []
    assert body["stop_reason"] == "end_turn"
    [call] = body["tool_calls"]
    assert call["status"] == "error"
    assert MISSING_DELIVERY_MESSAGE in call["result"]
    assert placed == []


def test_tool_batched_with_an_order_is_reported_done_and_only_once(client, placed):
    """remember_fact runs at once, even though Strands parks its result until the order resumes."""
    model = ScriptedModel(
        [
            tool_call("remember_fact", key="delivery_location", value="Lab 3.12"),
            tool_call("create_purchase_order", lines=LINES, deliver_to="Lab 3.12"),
        ],
        "Placed PO-000001.",
    )
    body = propose(client, model)
    calls = [(c["name"], c["status"]) for c in body["tool_calls"]]
    assert calls == [("remember_fact", "success"), ("create_purchase_order", None)]

    approved = decide(client, model, body, "approve").json()
    calls = [(c["name"], c["status"]) for c in approved["tool_calls"]]
    assert calls == [("create_purchase_order", "success")]
    assert len(placed) == 1
