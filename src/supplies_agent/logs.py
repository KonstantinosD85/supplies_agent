"""Logging setup shared by every service. Call configure_logging() first in each main().

Log lines are structured: the message names the event and the fields go in `extra`,

    logger.info("invocation finished", extra={"user_id": ctx.user_id, "stop_reason": ...})

so a log backend (the agent exports to Loki) stores each field as its own searchable attribute.
On the console the fields read in the Strands style, `field=<value>, other=<value> | message`,
so our lines and the SDK's look the same in one stream. A service with tracing adds
` trace_id=<...>` after the logger name, so a line can be matched with its trace.
"""

import logging
import os
from collections.abc import Callable

# Attributes every LogRecord has; anything else on a record came from `extra`.
_RECORD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def fields(record: logging.LogRecord) -> dict[str, object]:
    """The structured fields of a record: what the caller passed in `extra`."""
    return {k: v for k, v in vars(record).items() if k not in _RECORD_ATTRS}


class ConsoleFormatter(logging.Formatter):
    """`<time> <LEVEL> <logger>[ trace_id=<...>] | field=<value>, ... | message`."""

    def __init__(self, trace: Callable[[], str]) -> None:
        super().__init__()
        self.trace = trace

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 (logging API)
        extras = ", ".join(f"{k}=<{v}>" for k, v in fields(record).items())
        head = f"{record.asctime} {record.levelname} {record.name}{self.trace()}"
        return f"{head} | {extras} | {record.message}" if extras else f"{head} | {record.message}"

    def usesTime(self) -> bool:  # noqa: N802 (logging API)
        return True


def configure_logging(trace: Callable[[], str] | None = None) -> None:
    """Our code at LOG_LEVEL (default INFO), the Strands SDK at STRANDS_LOG_LEVEL (default WARNING).

    Strands logs mostly at DEBUG and does not use INFO, so WARNING keeps it quiet until needed.
    Other libraries stay at WARNING; uvicorn configures its own loggers.

    trace returns the text that follows the logger name on each console line (e.g.
    " trace_id=<...>" for the current request); services without tracing pass nothing.
    """
    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter(trace or (lambda: "")))
    logging.basicConfig(handlers=[console], level=logging.WARNING)
    logging.getLogger("supplies_agent").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logging.getLogger("strands").setLevel(os.environ.get("STRANDS_LOG_LEVEL", "WARNING").upper())
