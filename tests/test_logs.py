import logging

import pytest

from supplies_agent.logs import configure_logging


@pytest.fixture(autouse=True)
def restore_levels():
    loggers = [logging.getLogger(name) for name in ("supplies_agent", "strands")]
    levels = [logger.level for logger in loggers]
    factory = logging.getLogRecordFactory()
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)
    logging.setLogRecordFactory(factory)


def format_line(message: str) -> str:
    record = logging.getLogger("supplies_agent.test").makeRecord(
        "supplies_agent.test", logging.INFO, __file__, 1, message, (), None
    )
    return logging.Formatter("%(name)s%(trace)s | %(message)s").format(record)


def test_without_tracing_lines_have_no_trace_field():
    configure_logging()
    assert format_line("hello") == "supplies_agent.test | hello"


def test_a_service_with_tracing_adds_the_trace_after_the_logger_name():
    configure_logging(trace=lambda: " trace_id=<abc>")
    assert format_line("hello") == "supplies_agent.test trace_id=<abc> | hello"
    configure_logging(trace=lambda: " trace_id=<def>")  # reconfiguring replaces, never stacks
    assert format_line("hello") == "supplies_agent.test trace_id=<def> | hello"


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
