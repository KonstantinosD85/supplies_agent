"""Long-term memory: a few facts per user (lab, team, delivery location) stored in SQLite.

Implements the Strands memory SDK's MemoryStore, so a MemoryManager injects the profile into
the model's input and routes writes here through MemoryManager.add. Each fact is one value per
key: a write carries the key in its metadata and replaces the previous value. On AgentCore this
is replaced by AgentCore Memory.

Public API, the only names the agent service uses: FACT_KEYS, STORE_NAME, setup(), store().
"""

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strands.memory import MemoryEntry, SearchOptions

__all__ = ["FACT_KEYS", "STORE_NAME", "SqliteFactStore", "setup", "store"]

logger = logging.getLogger(__name__)

STORE_NAME = "user_profile"
# The facts kept about a user. A closed set, so the profile can't grow into free-form text
# that is replayed into every future prompt.
FACT_KEYS = ("lab", "team", "delivery_location")
MAX_VALUE_CHARS = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_facts (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def setup(db_path: str) -> None:
    """Create the user facts table at service startup. Facts are kept until overwritten."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as conn, conn:
        conn.executescript(SCHEMA)
    logger.info("user fact storage ready")


class SqliteFactStore:
    """The profile of one user, as a Strands MemoryStore.

    The store is bound to its user when it is built, so reads and writes can only ever touch
    that user's facts: nothing the model sends carries a user id.
    """

    name = STORE_NAME
    description = "Lasting facts about the current user: lab, team and delivery location."
    max_search_results = len(FACT_KEYS)
    writable = True
    extraction = None  # facts are written explicitly, never extracted by a model

    def __init__(self, db_path: str, user_id: str) -> None:
        self.db_path = db_path
        self.user_id = user_id

    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        """The whole profile: it is a handful of rows, so the query doesn't narrow it."""
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_facts WHERE user_id = ? ORDER BY key",
                (self.user_id,),
            ).fetchall()
        return [
            MemoryEntry(content=f"{row['key']}: {row['value']}", metadata={"key": row["key"]})
            for row in rows
        ]

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Set one fact: metadata["key"] names it, content is its new value."""
        key = (metadata or {}).get("key")
        if key not in FACT_KEYS:
            raise ValueError(f"Unknown fact key {key!r}. Use one of: {', '.join(FACT_KEYS)}.")
        value = " ".join(content.split())  # one line, so it renders cleanly in the prompt
        if not value or len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"A fact value must be 1 to {MAX_VALUE_CHARS} characters.")
        with closing(_connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO user_facts (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (user_id, key)"
                " DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (self.user_id, key, value, datetime.now(UTC).isoformat()),
            )
        logger.info("user fact saved", extra={"user_id": self.user_id, "key": key})


def store(db_path: str, user_id: str) -> SqliteFactStore:
    """The memory store holding this user's facts."""
    return SqliteFactStore(db_path, user_id)
