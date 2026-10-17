"""The /invocations and /ping endpoints with a scripted model and local stand-in tools."""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from scripted_model import ScriptedModel, tool_call
from strands.types.exceptions import MCPClientInitializationError

from supplies_agent.agent.prompts import SYSTEM_PROMPT
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER

REQUEST = {"message": "Do we have PBS?"}
HEADERS = {USER_HEADER: "alice", SESSION_HEADER: "session-1"}


def test_chat_runs_tools_and_reports_them(client_for):
    model = ScriptedModel(tool_call("check_stock", query="PBS"), "You have 6 packs of PBS.")
    response = client_for(model).post("/invocations", json=REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "You have 6 packs of PBS."
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 20, "output_tokens": 10}
    [call] = body["tool_calls"]
    assert (call["name"], call["input"], call["status"]) == (
        "check_stock",
        {"query": "PBS"},
        "success",
    )
    assert "BL-PBS-500" in call["result"]

    assert model.calls[0]["system_prompt"] == SYSTEM_PROMPT
    # The second model call sees the tool result.
    assert "BL-PBS-500" in str(model.calls[1]["messages"][-1])


def test_identity_reaches_the_tool_connection(client_for):
    seen: list = []
    client_for(ScriptedModel("Hi."), seen=seen).post("/invocations", json=REQUEST)
    assert seen == [("alice", "session-1")]


def test_ping_follows_the_agentcore_contract(client_for):
    response = client_for(ScriptedModel()).get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


def test_accepts_agentcore_length_session_ids(client_for):
    seen: list = []
    session = "a" * 40  # AgentCore runtimeSessionId is at least 33 characters
    client_for(ScriptedModel("Hi."), seen=seen).post(
        "/invocations", json=REQUEST, headers={SESSION_HEADER: session}
    )
    assert seen == [("alice", session)]


@pytest.mark.parametrize("header", [USER_HEADER, SESSION_HEADER])
def test_identity_headers_are_required(client_for, header):
    client = TestClient(client_for(ScriptedModel()).app)  # no default headers
    headers = {k: v for k, v in HEADERS.items() if k != header}
    response = client.post("/invocations", json=REQUEST, headers=headers)
    assert response.status_code == 422
    assert header in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {USER_HEADER: "alice smith"},  # space
        {SESSION_HEADER: "bad id!"},
        {SESSION_HEADER: "x" * 101},
    ],
)
def test_invalid_identity_headers_are_rejected(client_for, headers):
    response = client_for(ScriptedModel()).post("/invocations", json=REQUEST, headers=headers)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": "hi", "user_id": "alice"},  # ids are not accepted in the body
        {},
    ],
)
def test_invalid_bodies_are_rejected(client_for, body):
    response = client_for(ScriptedModel()).post("/invocations", json=body)
    assert response.status_code == 422


def test_unreachable_tools_return_503(app_for):
    @contextmanager
    def broken(settings, ctx):
        raise MCPClientInitializationError("connection refused")
        yield

    response = TestClient(app_for(ScriptedModel(), broken), headers=HEADERS).post(
        "/invocations", json=REQUEST
    )
    assert response.status_code == 503
    assert "try again" in response.json()["detail"]


def test_root_serves_the_web_ui(client_for):
    response = client_for(ScriptedModel()).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/invocations" in response.text
