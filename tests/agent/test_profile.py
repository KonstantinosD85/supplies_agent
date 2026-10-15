"""User facts (long-term memory) through /invocations: remember_fact, injection and isolation."""

from typing import get_args

from scripted_model import ScriptedModel, tool_call

from supplies_agent.agent.profile import FactKey, remember_fact
from supplies_agent.memory import facts

REQUEST = {"message": "Order 2 boxes of gloves"}
REMEMBER = tool_call("remember_fact", key="delivery_location", value="Lab 3.12")


def model_input(model: ScriptedModel, call: int = 0) -> str:
    return str(model.calls[call]["messages"])


def test_tool_keys_match_the_store_and_carry_no_user_id():
    assert get_args(FactKey) == facts.FACT_KEYS
    schema = remember_fact.tool_spec["inputSchema"]["json"]
    assert set(schema["properties"]) == {"key", "value"}
    assert schema["properties"]["key"]["enum"] == list(facts.FACT_KEYS)


def test_remembered_fact_is_injected_in_a_new_session(client_for):
    body = client_for(ScriptedModel(REMEMBER, "Saved."), session_id="s1").post(
        "/invocations", json={"message": "Deliver my orders to Lab 3.12"}
    )
    [call] = body.json()["tool_calls"]
    assert (call["name"], call["status"]) == ("remember_fact", "success")

    model = ScriptedModel("Ordering for Lab 3.12.")
    client_for(model, session_id="s2").post("/invocations", json=REQUEST)
    assert "<memory>" in model_input(model)
    assert "delivery_location: Lab 3.12" in model_input(model)


def test_other_users_never_see_the_fact(client_for):
    client_for(ScriptedModel(REMEMBER, "Saved."), session_id="s1").post(
        "/invocations", json=REQUEST
    )
    model = ScriptedModel("Where should I deliver?")
    client_for(model, user_id="bob", session_id="s2").post("/invocations", json=REQUEST)
    assert "<memory>" not in model_input(model)
    assert "Lab 3.12" not in model_input(model)


def test_injected_facts_stay_out_of_the_session_history(client_for):
    client_for(ScriptedModel(REMEMBER, "Saved."), session_id="s1").post(
        "/invocations", json=REQUEST
    )
    client_for(ScriptedModel("First."), session_id="s2").post("/invocations", json=REQUEST)

    model = ScriptedModel("Second.")
    client_for(model, session_id="s2").post("/invocations", json=REQUEST)
    # Only the newest user message carries the profile; the stored first one does not.
    assert model_input(model).count("<memory>") == 1


def test_an_invalid_fact_is_returned_to_the_model_with_the_reason(client_for):
    model = ScriptedModel(tool_call("remember_fact", key="lab", value="x" * 101), "Sorry.")
    body = client_for(model).post("/invocations", json=REQUEST).json()

    [call] = body["tool_calls"]
    assert call["status"] == "error"
    assert "1 to 100 characters" in call["result"]
