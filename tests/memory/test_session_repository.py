import sqlite3

import pytest
from strands.types.session import Session, SessionAgent, SessionMessage, SessionType

from supplies_agent.memory import sessions
from supplies_agent.memory.sessions import SessionAccessDenied, SqliteSessionRepository


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "agent.db")
    sessions.setup(path, retention_days=30)
    return path


@pytest.fixture
def repo(db_path) -> SqliteSessionRepository:
    repo = SqliteSessionRepository(db_path, "alice")
    repo.create_session(Session(session_id="s1", session_type=SessionType.AGENT))
    repo.create_agent("s1", SessionAgent(agent_id="a1", state={}, conversation_manager_state={}))
    return repo


def message(i: int, text: str) -> SessionMessage:
    return SessionMessage(message={"role": "user", "content": [{"text": text}]}, message_id=i)


def test_session_round_trip(repo):
    assert repo.read_session("s1").session_id == "s1"
    assert repo.read_session("missing") is None


def test_session_owned_by_someone_else_is_denied(db_path, repo):
    with pytest.raises(SessionAccessDenied):
        SqliteSessionRepository(db_path, "mallory").read_session("s1")


def test_agent_state_round_trip(repo):
    agent = repo.read_agent("s1", "a1")
    agent.state = {"pending": "approval"}
    repo.update_agent("s1", agent)
    assert repo.read_agent("s1", "a1").state == {"pending": "approval"}
    assert repo.read_agent("s1", "other") is None


def test_messages_are_listed_in_order_with_paging(repo):
    for i, text in enumerate(["a", "b", "c"]):
        repo.create_message("s1", "a1", message(i, text))

    def texts(**kwargs):
        listed = repo.list_messages("s1", "a1", **kwargs)
        return [m.message["content"][0]["text"] for m in listed]

    assert texts() == ["a", "b", "c"]
    assert texts(offset=1) == ["b", "c"]
    assert texts(limit=1, offset=1) == ["b"]


def test_message_update_keeps_redaction(repo):
    repo.create_message("s1", "a1", message(0, "secret"))
    stored = repo.read_message("s1", "a1", 0)
    stored.redact_message = {"role": "user", "content": [{"text": "[redacted]"}]}
    repo.update_message("s1", "a1", stored)
    assert repo.read_message("s1", "a1", 0).to_message()["content"][0]["text"] == "[redacted]"


def test_purge_removes_only_inactive_sessions(db_path, repo):
    other = SqliteSessionRepository(db_path, "bob")
    other.create_session(Session(session_id="s2", session_type=SessionType.AGENT))
    repo.create_message("s1", "a1", message(0, "old"))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET updated_at = '2000-01-01' WHERE session_id = 's1'")

    assert sessions.setup(db_path, retention_days=30) == 1  # purge runs at startup
    assert repo.read_session("s1") is None
    assert repo.list_messages("s1", "a1") == []  # messages go with their session
    assert other.read_session("s2") is not None
