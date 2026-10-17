"""Short-term memory through /invocations: history per session, restarts, isolation."""

import sqlite3
import threading
from contextlib import contextmanager

from fastapi.testclient import TestClient
from scripted_model import ScriptedModel, tool_call

from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER


def user_texts(messages) -> list[str]:
    return [
        block["text"]
        for message in messages
        if message["role"] == "user"
        for block in message["content"]
        if "text" in block
    ]


def test_conversation_continues_within_a_session(client_for):
    model = ScriptedModel("PBS is in stock.", "You asked about PBS.")
    client = client_for(model)
    client.post("/invocations", json={"message": "Do we have PBS?"})
    client.post("/invocations", json={"message": "What did I ask?"})

    assert user_texts(model.calls[1]["messages"]) == ["Do we have PBS?", "What did I ask?"]


def test_history_and_tool_results_survive_a_restart(client_for):
    before = ScriptedModel(tool_call("check_stock", query="PBS"), "6 packs of PBS.")
    client_for(before).post("/invocations", json={"message": "Do we have PBS?"})

    after = ScriptedModel("You have 6 packs.")  # a new app on the same database
    response = client_for(after).post("/invocations", json={"message": "How many again?"})

    history = after.calls[0]["messages"]
    assert user_texts(history) == ["Do we have PBS?", "How many again?"]
    assert "BL-PBS-500" in str(history)  # the earlier tool result is part of the history
    assert response.json()["tool_calls"] == []  # only this turn's tool calls are reported


def test_sessions_do_not_share_history(client_for):
    model = ScriptedModel("Hi.", "Hello.")
    client_for(model, session_id="session-1").post("/invocations", json={"message": "first"})
    client_for(model, session_id="session-2").post("/invocations", json={"message": "second"})

    assert user_texts(model.calls[1]["messages"]) == ["second"]


def test_another_users_session_is_rejected(client_for):
    client_for(ScriptedModel("Hi.")).post("/invocations", json={"message": "my secret order"})

    intruder = ScriptedModel("never called")
    response = client_for(intruder, user_id="mallory").post(
        "/invocations", json={"message": "what did alice say?"}
    )
    assert response.status_code == 403
    assert intruder.calls == []  # rejected before the model or tools are involved


def test_concurrent_turns_in_one_session_are_refused(app_for):
    entered, release = threading.Event(), threading.Event()

    @contextmanager
    def slow_tools(settings, ctx):
        entered.set()
        release.wait(timeout=5)
        yield []

    model = ScriptedModel("a", "b")
    client = TestClient(
        app_for(model, slow_tools), headers={USER_HEADER: "alice", SESSION_HEADER: "session-1"}
    )

    first = threading.Thread(
        target=client.post, args=("/invocations",), kwargs={"json": {"message": "1"}}
    )
    first.start()
    entered.wait(timeout=5)
    second = client.post("/invocations", json={"message": "2"})
    release.set()
    first.join()

    assert second.status_code == 409


def test_expired_sessions_are_purged_on_startup(client_for, settings):
    client_for(ScriptedModel("Hi.")).post("/invocations", json={"message": "old"})
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE sessions SET updated_at = '2000-01-01T00:00:00+00:00'")

    model = ScriptedModel("Fresh start.")
    client_for(model).post("/invocations", json={"message": "new"})  # new app: purge runs
    assert user_texts(model.calls[0]["messages"]) == ["new"]
