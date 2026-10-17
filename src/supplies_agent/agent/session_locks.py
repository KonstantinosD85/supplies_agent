"""One invocation at a time per session."""

import threading
from collections.abc import Generator
from contextlib import contextmanager


class SessionBusy(Exception):
    """Another invocation for this session is still running."""


class SessionLocks:
    """One invocation at a time per session, so two invocations never interleave their messages.

    Refuses rather than waits: a second message mid-invocation is almost always a double
    submit, and refusing can't tie up a worker thread.

    In-process only: enough for one agent-service process (AgentCore runs one per session).
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()

    @contextmanager
    def hold(self, session_id: str) -> Generator[None]:
        with self._lock:
            if session_id in self._active:
                raise SessionBusy(session_id)
            self._active.add(session_id)
        try:
            yield
        finally:
            with self._lock:
                self._active.discard(session_id)
