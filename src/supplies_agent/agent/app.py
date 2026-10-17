"""HTTP service: POST /invocations runs one agent invocation for a user's session.

Follows the AgentCore Runtime HTTP contract (POST /invocations, GET /ping, port 8080),
so the same container runs locally and on AgentCore. An invocation answers with JSON, or streams
as server-sent events when the client sends `Accept: text/event-stream`.
"""

import uvicorn
from fastapi import FastAPI

from supplies_agent.agent import errors, routes, telemetry
from supplies_agent.agent.session_locks import SessionLocks
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.logs import configure_logging
from supplies_agent.memory import facts, sessions


def create_app(settings: AgentSettings | None = None) -> FastAPI:
    settings = settings or AgentSettings()
    app = FastAPI(title="Lab supplies agent", telemetry=telemetry.FASTAPI_TELEMETRY)
    # Shared by every request, through dependencies.py.
    app.state.settings = settings
    app.state.locks = SessionLocks()

    sessions.setup(settings.db_path, settings.session_retention_days)
    facts.setup(settings.db_path)

    errors.register(app)
    app.include_router(routes.router)
    return app


def main() -> None:
    configure_logging(trace=telemetry.log_trace)
    telemetry.setup()
    settings = AgentSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
