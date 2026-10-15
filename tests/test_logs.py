import logging

import pytest

from supplies_agent.logs import configure_logging


@pytest.fixture(autouse=True)
def restore_levels():
    loggers = [logging.getLogger(name) for name in ("supplies_agent", "strands")]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def test_defaults_keep_strands_quiet(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("STRANDS_LOG_LEVEL", raising=False)
    configure_logging()
    assert logging.getLogger("supplies_agent").level == logging.INFO
    assert logging.getLogger("strands").level == logging.WARNING


def test_levels_come_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("STRANDS_LOG_LEVEL", "DEBUG")
    configure_logging()
    assert logging.getLogger("supplies_agent").level == logging.DEBUG
    assert logging.getLogger("strands").level == logging.DEBUG
