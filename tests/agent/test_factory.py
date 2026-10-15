from scripted_model import ScriptedModel
from strands import tool

from supplies_agent.agent.factory import build_agent
from supplies_agent.agent.prompts import SYSTEM_PROMPT
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER, RequestContext


@tool
def check_stock(query: str) -> dict:
    """Check stock."""
    return {}


def test_build_agent_wires_prompt_tools_and_identity():
    ctx = RequestContext.model_validate({USER_HEADER: "alice", SESSION_HEADER: "session-1"})
    agent = build_agent(ScriptedModel(), [check_stock], ctx)
    assert agent.system_prompt == SYSTEM_PROMPT
    assert agent.tool_names == ["check_stock"]
    assert agent.trace_attributes == {"user.id": "alice", "session.id": "session-1"}
