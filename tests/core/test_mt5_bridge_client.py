"""MT5BridgeClient's connection pool sizing and self-recycling watchdog.

Covers the 2026-07-23 incident: the shared httpx.AsyncClient's connection
pool wedged (leaked/stuck keep-alive sockets) while the bridge server
itself stayed reachable the whole time -- confirmed by a direct curl to
the bridge succeeding instantly while every request through this client
kept failing for 30+ minutes, surviving an app restart that didn't touch
this client. Fix: a larger pool (less likely to exhaust under multiple
concurrent browser tabs) plus a watchdog that tears down and rebuilds the
client once the tick failure streak crosses a threshold."""
from unittest.mock import AsyncMock, patch

import pytest

from forex_trader.core import mt5_bridge


@pytest.fixture(autouse=True)
def _reset_streak():
    mt5_bridge._tick_fail_streak = 0
    yield
    mt5_bridge._tick_fail_streak = 0


@pytest.fixture
def client():
    return mt5_bridge.MT5BridgeClient("http://localhost:9010")


@pytest.mark.asyncio
async def test_startup_uses_generous_pool_limits(client):
    await client.startup()
    try:
        limits = client._http._transport._pool._max_connections
        keepalive = client._http._transport._pool._max_keepalive_connections
        assert limits == 20
        assert keepalive == 10
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_fetch_tick_success_resets_streak(client):
    mt5_bridge._tick_fail_streak = 12
    fake_response = AsyncMock()
    fake_response.raise_for_status = lambda: None
    fake_response.json = lambda: {"bid": 4000.0, "ask": 4000.2, "timestamp": 0}
    with patch.object(client, "_request", AsyncMock(return_value=fake_response)):
        tick = await client._fetch_tick()
    assert tick is not None
    assert mt5_bridge._tick_fail_streak == 0


@pytest.mark.asyncio
async def test_fetch_tick_failure_increments_streak_without_recycling(client):
    with patch.object(client, "_request", AsyncMock(side_effect=Exception("boom"))), \
         patch.object(client, "_recycle_http_client", AsyncMock()) as recycle:
        for _ in range(5):
            await client._fetch_tick()
    assert mt5_bridge._tick_fail_streak == 5
    recycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_tick_recycles_client_at_threshold(client):
    with patch.object(client, "_request", AsyncMock(side_effect=Exception("boom"))), \
         patch.object(client, "_recycle_http_client", AsyncMock()) as recycle:
        for _ in range(mt5_bridge._CLIENT_RECYCLE_THRESHOLD):
            await client._fetch_tick()
    assert mt5_bridge._tick_fail_streak == mt5_bridge._CLIENT_RECYCLE_THRESHOLD
    recycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_tick_recycles_again_on_next_multiple(client):
    with patch.object(client, "_request", AsyncMock(side_effect=Exception("boom"))), \
         patch.object(client, "_recycle_http_client", AsyncMock()) as recycle:
        for _ in range(mt5_bridge._CLIENT_RECYCLE_THRESHOLD * 2):
            await client._fetch_tick()
    assert recycle.await_count == 2


@pytest.mark.asyncio
async def test_recycle_http_client_tears_down_and_rebuilds(client):
    await client.startup()
    old_http = client._http
    await client._recycle_http_client()
    assert client._http is not None
    assert client._http is not old_http
    await client.shutdown()


@pytest.mark.asyncio
async def test_recycle_http_client_guards_against_reentrancy(client):
    await client.startup()
    client._recycling = True
    with patch.object(client, "shutdown", AsyncMock()) as shutdown_mock:
        await client._recycle_http_client()
    shutdown_mock.assert_not_awaited()
    await client.shutdown()
