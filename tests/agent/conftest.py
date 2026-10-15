from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from strands import tool

from supplies_agent.agent.app import create_app
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER
from supplies_agent.agent.settings import AgentSettings


@tool
def check_stock(query: str) -> dict:
    """Check stock."""
    return {"matches": [{"sku": "BL-PBS-500", "quantity": 6}]}


@pytest.fixture
def settings(tmp_path) -> AgentSettings:
    return AgentSettings(db_path=str(tmp_path / "agent.db"))


@pytest.fixture
def client_for(settings):
    """Build a client whose agent uses the given model and a local stand-in check_stock tool.

    Each call creates a new app on the same database, like a service restart.
    """

    def make(
        model, *, user_id="alice", session_id="session-1", seen: list | None = None
    ) -> TestClient:
        @contextmanager
        def tool_source(settings, ctx):
            if seen is not None:
                seen.append((ctx.user_id, ctx.session_id))
            yield [check_stock]

        app = create_app(settings, model_factory=lambda _: model, tool_source=tool_source)
        return TestClient(app, headers={USER_HEADER: user_id, SESSION_HEADER: session_id})

    return make
