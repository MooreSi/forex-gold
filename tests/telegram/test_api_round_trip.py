"""Telegram's own round trip, for Settings > Latency (docs/todo/006).

One light request (help.getNearestDc), timed. It reads no messages and sends
none; what it adds is the network hop between this machine and the data
centre Telegram delivers our updates from.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.telegram.reader import AUTH_CONNECTED, TelegramReader


class _Nearest:
    this_dc = 4
    nearest_dc = 2
    country = "GB"


class _FakeClient:
    def __init__(self, delay: float = 0.0, fail: Exception | None = None):
        self.delay = delay
        self.fail = fail
        self.requests = []
        self.session = type("S", (), {"dc_id": 4})()

    async def __call__(self, request):
        self.requests.append(type(request).__name__)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return _Nearest()


def _reader(tmp_path, client):
    r = TelegramReader({"sessions_dir": str(tmp_path)})
    r._client = client
    r._auth_state = AUTH_CONNECTED
    return r


@pytest.mark.asyncio
async def test_it_times_one_light_request(tmp_path):
    client = _FakeClient(delay=0.01)
    out = await _reader(tmp_path, client).api_round_trip()

    assert client.requests == ["GetNearestDcRequest"]
    assert out["ok"] is True and out["ms"] >= 5.0
    assert out["session_dc"] == 4 and out["nearest_dc"] == 2


@pytest.mark.asyncio
async def test_a_failure_is_reported_not_raised(tmp_path):
    out = await _reader(tmp_path, _FakeClient(fail=ConnectionError("down"))).api_round_trip()

    assert out["ok"] is False and "down" in out["detail"]


@pytest.mark.asyncio
async def test_not_connected_sends_nothing(tmp_path):
    client = _FakeClient()
    r = _reader(tmp_path, client)
    r._auth_state = "disconnected"

    out = await r.api_round_trip()

    assert out["ok"] is False and client.requests == []
