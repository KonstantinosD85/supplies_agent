"""Fault injection: random 429s, 503s and slow responses, switched on via /admin/faults.

Every injected fault is marked with an X-Fault-Injected header so it shows up in traces.
"""

import asyncio
import logging
import random

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from supplies_agent.api.settings import FaultConfig

logger = logging.getLogger(__name__)

# Health checks, the admin switch and the docs never fail.
_EXEMPT_PREFIXES = ("/health", "/admin", "/docs", "/openapi.json")


def _log(kind: str, request: Request) -> None:
    logger.info(
        "fault=<%s>, method=<%s>, path=<%s> | fault injected",
        kind,
        request.method,
        request.url.path,
    )


def _fault(status: int, kind: str, detail: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        status_code=status,
        content={"detail": detail},
        headers={"X-Fault-Injected": kind, **(headers or {})},
    )


async def inject_faults(request: Request, call_next) -> Response:
    if request.url.path.startswith(_EXEMPT_PREFIXES):
        return await call_next(request)

    faults: FaultConfig = request.app.state.faults

    if random.random() < faults.rate_limit_rate:
        _log("rate_limit", request)
        return _fault(
            429, "rate_limit", "Rate limit exceeded. Retry after 1 second.", {"Retry-After": "1"}
        )

    if random.random() < faults.slow_rate:
        _log("slow", request)
        await asyncio.sleep(faults.slow_seconds)

    server_error = random.random() < faults.server_error_rate
    if server_error and request.method == "GET":
        _log("server_error", request)
        return _fault(503, "server_error", "Service temporarily unavailable. Retry later.")

    response = await call_next(request)

    if server_error:
        _log("server_error_after_write", request)
        # The write has already committed. The client only sees a 503, so a retry
        # without the same Idempotency-Key would create a duplicate.
        return _fault(
            503, "server_error_after_write", "Service temporarily unavailable. Retry later."
        )
    return response
