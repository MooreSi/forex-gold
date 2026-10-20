"""A tick-range read that hangs pauses tick-range reads instead of freezing
the bridge every thirty seconds.

2026-10-19, owner's Mac. At 15:11:13 MT5 switched broker access point and
reconnected. From that second `copy_ticks_range` never answered again, for
any window, while bars, the account and positions answered in milliseconds.
A hung native call freezes the whole Wine-side bridge, /health included, so
the watchdog read every hang as an outage and tore Wine down. The reversal
evidence collector asks for a tick range every 30s, so every fresh terminal
froze within a minute of starting: 46 restarts in two and a half hours.

`get_ticks_range` already returns [] on any failure, and every caller
already copes with that. What changes is only that, after a read times out
(or its caller gives up on it), the next ones return that same [] at once
for a while instead of queuing another frozen call on the bridge.

NOTHING HERE REACHES A BRIDGE OR A BROKER. The client's HTTP helper is
replaced by fakes; no order path is involved -- /ticks is read-only.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend.src.services.broker import mt5_client as mc


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    # Not mc.time.monotonic: that is the shared time module, and asyncio's
    # own clock stops with it.
    monkeypatch.setattr(mc, "_ticks_clock", c)
    return c


def _client(behaviour):
    """A client whose every request runs `behaviour()` and counts calls."""
    client = mc.MT5BridgeClient("http://localhost:9010")
    calls = []

    async def _request(method, url, timeout=10.0, **kwargs):
        calls.append(url)
        return await behaviour()

    client._request = _request
    return client, calls


def _ok(ticks):
    async def behaviour():
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"ticks": ticks})
    return behaviour


async def _times_out():
    raise httpx.ReadTimeout("timed out")


async def _refused():
    raise httpx.ConnectError("refused")


@pytest.mark.asyncio
class TestAHungReadPausesTheNextOnes:
    async def test_a_timed_out_read_stops_the_next_one_reaching_the_bridge(self, clock):
        client, calls = _client(_times_out)

        assert await client.get_ticks_range(1.0, 2.0) == []
        assert await client.get_ticks_range(1.0, 2.0) == []

        assert len(calls) == 1

    async def test_a_caller_giving_up_counts_as_a_hang(self, clock):
        """The evidence collector wraps the read in a 15s wait_for. The bridge
        is still stuck on the native call after the caller has left."""
        async def _never():
            await asyncio.sleep(3600)
        client, calls = _client(_never)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.get_ticks_range(1.0, 2.0), timeout=0.01)
        # Bounded so a missing pause fails here instead of hanging the suite.
        assert await asyncio.wait_for(client.get_ticks_range(1.0, 2.0), timeout=1) == []

        assert len(calls) == 1

    async def test_reads_resume_once_the_pause_is_over(self, clock):
        state = {"hang": True}

        async def behaviour():
            if state["hang"]:
                return await _times_out()
            return await _ok([{"time": 1.5}])()
        client, calls = _client(behaviour)

        await client.get_ticks_range(1.0, 2.0)
        state["hang"] = False
        clock.now += mc._TICKS_PAUSE_MIN_S + 1

        assert await client.get_ticks_range(1.0, 2.0) == [{"time": 1.5}]
        assert len(calls) == 2

    async def test_the_pause_doubles_while_it_keeps_hanging_up_to_a_ceiling(self, clock):
        client, calls = _client(_times_out)
        pauses = []
        for _ in range(8):
            await client.get_ticks_range(1.0, 2.0)
            pauses.append(client._ticks_paused_until - clock.now)
            clock.now = client._ticks_paused_until + 1

        assert pauses[0] == mc._TICKS_PAUSE_MIN_S
        assert pauses[1] == 2 * mc._TICKS_PAUSE_MIN_S
        assert max(pauses) == mc._TICKS_PAUSE_MAX_S
        assert len(calls) == 8

    async def test_a_good_read_resets_the_pause(self, clock):
        state = {"hang": True}

        async def behaviour():
            if state["hang"]:
                return await _times_out()
            return await _ok([])()
        client, _ = _client(behaviour)

        await client.get_ticks_range(1.0, 2.0)
        clock.now = client._ticks_paused_until + 1
        state["hang"] = False
        await client.get_ticks_range(1.0, 2.0)
        state["hang"] = True
        await client.get_ticks_range(1.0, 2.0)

        assert client._ticks_paused_until - clock.now == mc._TICKS_PAUSE_MIN_S


@pytest.mark.asyncio
class TestOnlyAHangPauses:
    async def test_a_refused_connection_does_not_pause(self, clock):
        """Refused means the bridge is restarting -- fast, and nothing is
        stuck on the other end."""
        client, calls = _client(_refused)

        await client.get_ticks_range(1.0, 2.0)
        await client.get_ticks_range(1.0, 2.0)

        assert len(calls) == 2

    async def test_a_missing_endpoint_is_still_raised(self, clock):
        async def _404():
            return SimpleNamespace(status_code=404)
        client, _ = _client(_404)

        with pytest.raises(RuntimeError, match="no /ticks endpoint"):
            await client.get_ticks_range(1.0, 2.0)

    async def test_the_pause_is_per_client(self, clock):
        hung, _ = _client(_times_out)
        fresh, calls = _client(_ok([]))

        await hung.get_ticks_range(1.0, 2.0)
        await fresh.get_ticks_range(1.0, 2.0)

        assert len(calls) == 1


@pytest.mark.asyncio
class TestTickAtSharesThePause:
    """/tick_at reads the same tick history (copy_ticks_from) and hung the
    same way on 2026-10-19, after the first fix had paused /ticks alone. The
    fill-cost and entry-spread reports call it with an 8s timeout, and each
    call froze the bridge for about a minute."""

    async def test_a_hung_tick_at_pauses_tick_at_and_tick_ranges(self, clock):
        client, calls = _client(_times_out)

        assert await client.get_tick_at(1.0) is None
        assert await client.get_tick_at(1.0) is None
        assert await client.get_ticks_range(1.0, 2.0) == []

        assert len(calls) == 1

    async def test_a_hung_tick_range_pauses_tick_at(self, clock):
        client, calls = _client(_times_out)

        await client.get_ticks_range(1.0, 2.0)
        assert await client.get_tick_at(1.0) is None

        assert len(calls) == 1

    async def test_tick_at_answers_normally_when_nothing_hangs(self, clock):
        async def behaviour():
            return SimpleNamespace(status_code=200, json=lambda: {"bid": 1.0})
        client, calls = _client(behaviour)

        assert await client.get_tick_at(1.0) == {"bid": 1.0}
        assert await client.get_tick_at(1.0) == {"bid": 1.0}
        assert len(calls) == 2

    async def test_a_refused_tick_at_does_not_pause(self, clock):
        client, calls = _client(_refused)

        await client.get_tick_at(1.0)
        await client.get_tick_at(1.0)

        assert len(calls) == 2
