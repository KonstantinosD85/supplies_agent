"""HTTP client for the supplies API, with retries, backoff and an overall timeout."""

import asyncio
import logging
import random
import time
from typing import Any

import httpx2
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from supplies_agent.mcp_server.settings import McpSettings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)  # a no-op until telemetry.setup() sets a provider

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiUnavailable(Exception):
    """Every attempt failed with a transient error, or the overall timeout ran out."""

    def __init__(self, attempts: int, last_error: str) -> None:
        super().__init__(f"gave up after {attempts} attempt(s); last error: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


class ApiRejected(Exception):
    """The API refused the request (4xx other than 429). Retrying will not help."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class SuppliesApi:
    def __init__(self, settings: McpSettings, transport: httpx2.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport  # Tests inject one; production uses the network.

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        """Send a request, retrying transient failures.

        Retries 429/5xx, connection errors and slow attempts with exponential backoff
        (honouring Retry-After), within max_attempts and total_timeout. POSTs are only
        safe to retry because every write carries an Idempotency-Key.
        """
        s = self._settings
        deadline = time.monotonic() + s.total_timeout
        attempt, last_error = 0, ""

        async with httpx2.AsyncClient(
            base_url=s.api_url, headers={"X-API-Key": s.api_key}, transport=self._transport
        ) as http:
            while attempt < s.max_attempts and time.monotonic() < deadline:
                attempt += 1
                retry_after: float | None = None
                # One span per attempt, so retries and injected faults show up in the trace.
                with tracer.start_as_current_span(
                    f"supplies API {method}",
                    kind=SpanKind.CLIENT,
                    attributes={
                        "http.request.method": method,
                        "url.path": path,
                        "supplies.attempt": attempt,
                    },
                ) as span:
                    try:
                        # asyncio.timeout bounds the whole attempt, whatever the transport does.
                        async with asyncio.timeout(
                            min(s.attempt_timeout, deadline - time.monotonic())
                        ):
                            response = await http.request(
                                method, path, params=params, json=json, headers=headers
                            )
                    except TimeoutError:
                        last_error = "no response within the attempt timeout"
                    except httpx2.TransportError as e:
                        last_error = f"{type(e).__name__}: {e}"
                    else:
                        span.set_attribute("http.response.status_code", response.status_code)
                        if fault := response.headers.get("X-Fault-Injected"):
                            span.set_attribute("supplies.fault_injected", fault)
                        if response.status_code not in RETRYABLE_STATUS:
                            if response.is_success:
                                return response
                            raise ApiRejected(response.status_code, _detail(response))
                        last_error = f"HTTP {response.status_code}"
                        retry_after = _retry_after(response)
                    span.set_status(StatusCode.ERROR, last_error)

                if attempt < s.max_attempts:
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                    logger.warning(
                        "supplies API call failed, retrying",
                        extra={
                            "method": method,
                            "path": path,
                            "attempt": attempt,
                            "error": last_error,
                            "retry_in_s": round(delay, 2),
                        },
                    )
                    await asyncio.sleep(delay)

        logger.warning(
            "supplies API unavailable, giving up",
            extra={"method": method, "path": path, "attempts": attempt, "error": last_error},
        )
        raise ApiUnavailable(attempt, last_error)

    def _backoff(self, attempt: int) -> float:
        base = self._settings.backoff_base * 2 ** (attempt - 1)
        return base + random.uniform(0, self._settings.backoff_base)  # jitter


def _retry_after(response: httpx2.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def _detail(response: httpx2.Response) -> str:
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return response.text
    # FastAPI validation errors are a list of {loc, msg, ...}.
    if isinstance(detail, list):
        return "; ".join(f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg')}" for d in detail)
    return str(detail)
