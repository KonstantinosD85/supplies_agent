from scripted_model import ScriptedModel
from strands import tool

from supplies_agent.agent.factory import build_agent
from supplies_agent.agent.prompts import SYSTEM_PROMPT
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER, RequestContext
from supplies_agent.memory import facts, sessions


@tool
def check_stock(query: str) -> dict:
    """Check stock."""
    return {}


def test_build_agent_wires_prompt_tools_identity_and_session(tmp_path):
    db_path = str(tmp_path / "agent.db")
    sessions.setup(db_path, retention_days=30)
    ctx = RequestContext.model_validate({USER_HEADER: "alice", SESSION_HEADER: "session-1"})
    session_manager = sessions.session_manager(db_path, ctx.user_id, ctx.session_id)

    fact_store = facts.store(db_path, ctx.user_id)

    agent = build_agent(ScriptedModel(), [check_stock], ctx, session_manager, fact_store)
    assert agent.system_prompt == SYSTEM_PROMPT
    # The MCP tools plus remember_fact; the memory SDK's own search/add tools stay off.
    assert sorted(agent.tool_names) == ["check_stock", "remember_fact"]
    assert agent.trace_attributes == {"user.id": "alice", "session.id": "session-1"}
    assert agent.agent_id == "supplies-agent"
    # The agent is registered in the session store.
    repo = sessions.SqliteSessionRepository(db_path, "alice")
    assert repo.read_agent("session-1", "supplies-agent") is not None
