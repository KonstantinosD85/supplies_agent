"""Logging setup shared by every service. Call configure_logging() first in each main().

Messages follow the Strands style, `field=<value>, other=<value> | message`, so our lines
and the SDK's read the same in one stream. A service with tracing adds ` trace_id=<...>` after
the logger name, so a line can be matched with its trace.
"""

import logging
import os
from collections.abc import Callable

FORMAT = "%(asctime)s %(levelname)s %(name)s%(trace)s | %(message)s"

_base_record_factory = logging.getLogRecordFactory()


def configure_logging(trace: Callable[[], str] | None = None) -> None:
    """Our code at LOG_LEVEL (default INFO), the Strands SDK at STRANDS_LOG_LEVEL (default WARNING).

    Strands logs mostly at DEBUG and does not use INFO, so WARNING keeps it quiet until needed.
    Other libraries stay at WARNING; uvicorn configures its own loggers.

    trace returns the text that follows the logger name on each line (e.g. " trace_id=<...>"
    for the current request); services without tracing pass nothing.
    """
    current_trace = trace or (lambda: "")

    def record_factory(*args, **kwargs) -> logging.LogRecord:
        record = _base_record_factory(*args, **kwargs)
        record.trace = current_trace()
        return record

    logging.setLogRecordFactory(record_factory)
    logging.basicConfig(format=FORMAT, level=logging.WARNING)
    logging.getLogger("supplies_agent").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logging.getLogger("strands").setLevel(os.environ.get("STRANDS_LOG_LEVEL", "WARNING").upper())
