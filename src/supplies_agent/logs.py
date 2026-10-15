"""Logging setup shared by every service. Call configure_logging() first in each main().

Messages follow the Strands style, `field=<value>, other=<value> | message`, so our lines
and the SDK's read the same in one stream.
"""

import logging
import os

FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"


def configure_logging() -> None:
    """Our code at LOG_LEVEL (default INFO), the Strands SDK at STRANDS_LOG_LEVEL (default WARNING).

    Strands logs mostly at DEBUG and does not use INFO, so WARNING keeps it quiet until needed.
    Other libraries stay at WARNING; uvicorn configures its own loggers.
    """
    logging.basicConfig(format=FORMAT, level=logging.WARNING)
    logging.getLogger("supplies_agent").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logging.getLogger("strands").setLevel(os.environ.get("STRANDS_LOG_LEVEL", "WARNING").upper())
