"""The VPS leg of Settings > Latency, carried on the existing ping/pong (docs/todo/006).

A ping with a `probe_id` asks the VPS for two answers: an immediate pong
echoing the id (that is the round trip) and a second pong carrying the VPS's
own latency report. No new message type, so either side can be the older
build:

  * an older VPS answers a plain pong, which the Mac reports as "update the
    VPS" rather than timing someone else's ping;
  * an older Mac never sends a probe_id, and a plain ping is answered exactly
    as before.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import _latency_sync as ls
from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss
from backend.src.utils import latency_trace as lt

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.fixture
def vps(monkeypatch):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    monkeypatch.setattr(srv, "_apply_peer_clock_offset", lambda msg: None, raising=False)

    async def _report(engine, reader):
        return {"probes": {"bridge": {"ms": 3.0}}}
    monkeypatch.setattr(ls, "_local_report", _report)
    return srv


class TestTheVpsSide:
    async def test_a_probe_is_echoed_at_once_then_answered_with_a_report(self, vps):
        ws = _Ws()

        await vps._dispatch(ws, {"type": P.MSG_PING, "probe_id": "abc"})
        assert ws.sent == [{"type": P.MSG_PONG, "probe_id": "abc"}]

        await asyncio.sleep(0.05)
        assert ws.sent[1]["probe_id"] == "abc"
        assert ws.sent[1]["latency"] == {"probes": {"bridge": {"ms": 3.0}}}

    async def test_a_plain_ping_is_answered_exactly_as_before(self, vps):
        ws = _Ws()

        await vps._dispatch(ws, {"type": P.MSG_PING})
        await asyncio.sleep(0.05)

        assert ws.sent == [{"type": P.MSG_PONG}]

    async def test_a_failed_report_is_still_an_answer(self, vps, monkeypatch):
        async def _boom(engine, reader):
            raise RuntimeError("no db")
        monkeypatch.setattr(ls, "_local_report", _boom)
        ws = _Ws()

        await vps._dispatch(ws, {"type": P.MSG_PING, "probe_id": "x"})
        await asyncio.sleep(0.05)

        assert "no db" in ws.sent[1]["latency"]["error"]


class _MacWs:
    """Plays the VPS: answers each probe ping the way `answer` says."""

    def __init__(self, client, answer):
        self.client = client
        self.answer = answer
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("type") == P.MSG_PING:
            asyncio.get_running_loop().create_task(self.answer(self.client, msg))


def _mac(answer):
    c = sc.SyncClient()
    c.conn_state = sc.CONN_CONNECTED
    c._ws = _MacWs(c, answer)
    return c


class TestTheMacSide:
    async def test_the_round_trip_and_the_report_come_back(self):
        async def _new_vps(client, ping):
            await asyncio.sleep(0.01)
            await client._dispatch({"type": P.MSG_PONG, "probe_id": ping["probe_id"]})
            await client._dispatch({"type": P.MSG_PONG, "probe_id": ping["probe_id"],
                                    "latency": {"probes": {}}})

        out = await _mac(_new_vps).probe_peer(timeout=1.0)

        assert out["ok"] is True
        assert 5.0 <= out["rtt_ms"] < 1000.0
        assert out["remote"] == {"probes": {}}

    async def test_an_older_vps_is_named_as_the_reason(self):
        async def _old_vps(client, ping):
            await client._dispatch({"type": P.MSG_PONG})

        out = await _mac(_old_vps).probe_peer(timeout=0.1)

        assert out["ok"] is False and out["rtt_ms"] is None
        assert "older version" in out["detail"]

    async def test_a_late_report_keeps_the_round_trip(self):
        async def _slow_report(client, ping):
            await client._dispatch({"type": P.MSG_PONG, "probe_id": ping["probe_id"]})

        out = await _mac(_slow_report).probe_peer(timeout=0.1)

        assert out["ok"] is True and out["rtt_ms"] is not None
        assert out["remote"] is None

    async def test_not_connected_sends_nothing(self):
        c = sc.SyncClient()

        out = await c.probe_peer(timeout=0.1)

        assert out["ok"] is False and out["detail"] == "VPS not connected"

    async def test_the_probe_ping_does_not_touch_the_clock(self):
        """A ping without an offset is how the server is told "not reported"
        -- a probe must not move the VPS's trading clock."""
        async def _new_vps(client, ping):
            await client._dispatch({"type": P.MSG_PONG, "probe_id": ping["probe_id"],
                                    "latency": {}})
        c = _mac(_new_vps)

        await c.probe_peer(timeout=0.1)

        assert c._ws.sent[0].get("clock_offset_min") is None


class TestForwardedOrdersAreTimedOnTheVps:
    async def test_the_vps_stamps_around_the_forwarded_open(self, monkeypatch):
        lt.clear()
        srv = ss.SyncServer.__new__(ss.SyncServer)

        class _Eng:
            async def open_trade(self, **kw):
                await asyncio.sleep(0.01)
                return {"trade_id": "t1"}

            async def get_fresh_tick(self):
                raise RuntimeError("no commentary in tests")
        srv._main_engine = _Eng()
        from backend.src.services.cluster import sync_repo
        monkeypatch.setattr(sync_repo, "mirror_insert_signal_if_absent", lambda *a: None)

        await srv._handle_signal_order(_Ws(), {"type": P.MSG_SIGNAL_ORDER,
                                              "signal_id": "sig-9", "direction": "BUY",
                                              "tg_source": "Breakout Engine"})

        (e,) = lt.entries("forwarded")
        assert e["label"] == "Breakout Engine"
        assert lt.gap_ms("fwd:sig-9", "f1_received", "f2_ordered") >= 5.0
        lt.clear()
