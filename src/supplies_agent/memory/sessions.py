"""Short-term memory: Strands sessions stored in SQLite, each owned by one user.

Implements Strands' SessionRepository, so RepositorySessionManager persists the conversation,
agent state and pending interrupts here. On AgentCore this is replaced by AgentCore Memory.

Public API, the only names the agent service uses: setup(), session_manager(),
SessionAccessDenied. Everything else is internal to this module.
"""

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage, SessionType

__all__ = ["SessionAccessDenied", "SqliteSessionRepository", "session_manager", "setup"]

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_agents (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id    TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id)
);
CREATE TABLE IF NOT EXISTS session_messages (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id    TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id, message_id)
);
"""


class SessionAccessDenied(Exception):
    """The session exists but belongs to another user."""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def setup(db_path: str, retention_days: int) -> int:
    """Prepare session storage at service startup. Returns how many expired sessions were purged.

    Creates the schema, then deletes sessions with no activity for retention_days
    (their agents and messages go with them).
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    with closing(_connect(db_path)) as conn, conn:
        conn.executescript(SCHEMA)
        purged = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,)).rowcount
    logger.info(
        "purged=<%d>, retention_days=<%d> | session storage ready, expired sessions removed",
        purged,
        retention_days,
    )
    return purged


class SqliteSessionRepository(SessionRepository):
    """Session storage for one user.

    Ownership is checked in read_session, which RepositorySessionManager calls before
    anything else: a session owned by someone else raises SessionAccessDenied.
    """

    def __init__(self, db_path: str, user_id: str) -> None:
        self.db_path = db_path
        self.user_id = user_id

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        with closing(_connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session.session_id, self.user_id, session.created_at, session.updated_at),
            )
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        if row["user_id"] != self.user_id:
            raise SessionAccessDenied(session_id)
        return Session(
            session_id=row["session_id"],
            session_type=SessionType.AGENT,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self._write(
            session_id,
            "INSERT INTO session_agents VALUES (?, ?, ?)",
            (session_id, session_agent.agent_id, json.dumps(session_agent.to_dict())),
        )

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT data FROM session_agents WHERE session_id = ? AND agent_id = ?",
                (session_id, agent_id),
            ).fetchone()
        return SessionAgent.from_dict(json.loads(row["data"])) if row else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        session_agent.updated_at = _now()
        self._write(
            session_id,
            "UPDATE session_agents SET data = ? WHERE session_id = ? AND agent_id = ?",
            (json.dumps(session_agent.to_dict()), session_id, session_agent.agent_id),
        )

    def create_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        self._write(
            session_id,
            "INSERT INTO session_messages VALUES (?, ?, ?, ?)",
            (
                session_id,
                agent_id,
                session_message.message_id,
                json.dumps(session_message.to_dict()),
            ),
        )

    def read_message(
        self, session_id: str, agent_id: str, message_id: int, **kwargs: Any
    ) -> SessionMessage | None:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT data FROM session_messages"
                " WHERE session_id = ? AND agent_id = ? AND message_id = ?",
                (session_id, agent_id, message_id),
            ).fetchone()
        return SessionMessage.from_dict(json.loads(row["data"])) if row else None

    def update_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        session_message.updated_at = _now()
        self._write(
            session_id,
            "UPDATE session_messages SET data = ?"
            " WHERE session_id = ? AND agent_id = ? AND message_id = ?",
            (
                json.dumps(session_message.to_dict()),
                session_id,
                agent_id,
                session_message.message_id,
            ),
        )

    def list_messages(
        self,
        session_id: str,
        agent_id: str,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SessionMessage]:
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT data FROM session_messages WHERE session_id = ? AND agent_id = ?"
                " ORDER BY message_id LIMIT ? OFFSET ?",
                (session_id, agent_id, -1 if limit is None else limit, offset),
            ).fetchall()
        return [SessionMessage.from_dict(json.loads(row["data"])) for row in rows]

    def _write(self, session_id: str, sql: str, params: tuple[Any, ...]) -> None:
        """Run one write and mark the session as active (for retention)."""
        with closing(_connect(self.db_path)) as conn, conn:
            conn.execute(sql, params)
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?", (_now(), session_id)
            )


def session_manager(db_path: str, user_id: str, session_id: str) -> RepositorySessionManager:
    """Load (or create) the user's session. Raises SessionAccessDenied for someone else's."""
    return RepositorySessionManager(
        session_id=session_id, session_repository=SqliteSessionRepository(db_path, user_id)
    )
