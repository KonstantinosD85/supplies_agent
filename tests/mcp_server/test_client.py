"""Retry, backoff and timeout behaviour of the supplies API client."""

import asyncio
import logging

import httpx2
import pytest

from supplies_agent.mcp_server.client import ApiRejected, ApiUnavailable, SuppliesApi

pytestmark = pytest.mark.anyio


def scripted(*responses):
    """A transport that returns the given responses in order and records each request."""
    calls: list[httpx2.Request] = []
    queue = list(responses)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        step = queue.pop(0)
        if isinstance(step, float):  # simulate a slow server
            await asyncio.sleep(step)
            return httpx2.Response(200, json={"slow": True})
        return step

    return httpx2.MockTransport(handler), calls


async def test_sends_api_key(mcp_settings):
    transport, calls = scripted(httpx2.Response(200, json=[]))
    await SuppliesApi(mcp_settings, transport).request("GET", "/stock")
    assert calls[0].headers["X-API-Key"] == "test-key"


async def test_retries_rate_limit_then_succeeds(mcp_settings):
    transport, calls = scripted(
        httpx2.Response(429, headers={"Retry-After": "0"}),
        httpx2.Response(503),
        httpx2.Response(200, json={"ok": True}),
    )
    response = await SuppliesApi(mcp_settings, transport).request("GET", "/stock")
    assert response.json() == {"ok": True}
    assert len(calls) == 3


async def test_slow_attempt_is_abandoned_and_retried(mcp_settings):
    transport, calls = scripted(1.0, httpx2.Response(200, json={"ok": True}))
    response = await SuppliesApi(mcp_settings, transport).request("GET", "/stock")
    assert response.json() == {"ok": True}
    assert len(calls) == 2


async def test_gives_up_after_max_attempts(mcp_settings):
    transport, calls = scripted(*[httpx2.Response(503)] * 4)
    with pytest.raises(ApiUnavailable) as e:
        await SuppliesApi(mcp_settings, transport).request("GET", "/stock")
    assert e.value.attempts == 4
    assert e.value.last_error == "HTTP 503"
    assert len(calls) == 4


async def test_gives_up_when_total_timeout_runs_out(mcp_settings):
    settings = mcp_settings.model_copy(update={"max_attempts": 100, "total_timeout": 0.5})
    transport, calls = scripted(*[1.0] * 100)
    with pytest.raises(ApiUnavailable):
        await SuppliesApi(settings, transport).request("GET", "/stock")
    assert len(calls) <= 3  # 0.2s per attempt within a 0.5s budget


async def test_client_errors_are_not_retried(mcp_settings):
    transport, calls = scripted(httpx2.Response(422, json={"detail": "Unknown SKU(s): X."}))
    with pytest.raises(ApiRejected) as e:
        await SuppliesApi(mcp_settings, transport).request("POST", "/purchase-orders", json={})
    assert (e.value.status, e.value.detail) == (422, "Unknown SKU(s): X.")
    assert len(calls) == 1


# --- Against the real supplies API with faults switched on ----------------------


async def test_write_that_fails_after_commit_is_never_duplicated(api):
    order = {"requested_by": "alice", "lines": [{"sku": "LC-GLOVE-M", "quantity": 1}]}
    headers = {"Idempotency-Key": "key-00000001"}

    await api.request("PUT", "/admin/faults", json={"server_error_rate": 1})
    # Every attempt commits (or replays) and then returns 503, so the client gives up...
    with pytest.raises(ApiUnavailable):
        await api.request("POST", "/purchase-orders", json=order, headers=headers)

    await api.request("PUT", "/admin/faults", json={})
    # ...but the order exists exactly once, and repeating it replays the same PO.
    retry = await api.request("POST", "/purchase-orders", json=order, headers=headers)
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert (await api.request("GET", "/purchase-orders")).json()["total"] == 1


async def test_retries_and_give_up_are_logged(mcp_settings, caplog):
    caplog.set_level(logging.INFO, logger="supplies_agent")
    transport, _ = scripted(*[httpx2.Response(503)] * 4)
    with pytest.raises(ApiUnavailable):
        await SuppliesApi(mcp_settings, transport).request("GET", "/stock")
    assert "attempt=<1>, error=<HTTP 503>" in caplog.text
    assert "attempts=<4>, error=<HTTP 503> | supplies API unavailable, giving up" in caplog.text
