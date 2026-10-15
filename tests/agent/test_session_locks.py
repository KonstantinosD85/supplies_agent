import pytest

from supplies_agent.agent.session_locks import SessionBusy, SessionLocks


def test_same_session_is_refused_while_held():
    locks = SessionLocks()
    with locks.hold("s1"), pytest.raises(SessionBusy):
        with locks.hold("s1"):
            pass


def test_other_sessions_run_in_parallel():
    locks = SessionLocks()
    with locks.hold("s1"), locks.hold("s2"):
        pass


def test_session_is_released_even_when_the_turn_fails():
    locks = SessionLocks()
    with pytest.raises(RuntimeError), locks.hold("s1"):
        raise RuntimeError("turn failed")
    with locks.hold("s1"):  # free again
        pass
