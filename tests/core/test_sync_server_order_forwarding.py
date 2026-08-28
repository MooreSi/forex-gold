"""The VPS placing trades on the Mac's behalf.

Under centralized signal generation the VPS has stopped analysing anything
itself, so a forwarded MSG_SIGNAL_ORDER is the ONLY source of new trades while
that mode is on. Two failures here are silent by nature, and both have already
happened:

  * Every forwarded trade failed with "FOREIGN KEY constraint failed" because
    the signal_id was created in the Mac's own vantage_signals table, never
    this node's. The fix mirrors a minimal row here FIRST. The VPS log shows
    those failures recurring for hours from 13:20 after centralized mode was
    switched on -- each one a missed trade, with nothing on screen to say so.
  * A handler that raises without acking leaves the Mac waiting on a timeout
    with no idea whether a trade was placed.

NO ORDER IS PLACED BY THESE TESTS. The engine is a fake that records the
arguments it was called with; nothing here touches a bridge, a broker, or the
real open_trade. The point is to check what the VPS is ASKED to do and in what
order, not to run it.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync.server import SyncServer
from backend.src.services.cluster.sync.protocol import (
    MSG_MARKET_ORDER_ACK, MSG_SIGNAL_ORDER_ACK,
)


pytestmark = [pytest.mark.usefixtures("fresh_db"), pytest.mark.asyncio]


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _FakeEngine:
    """Records calls. Places nothing."""

    def __init__(self, order_error=None):
        self.calls: list[tuple[str, dict]] = []
        self._order_error = order_error

    async def open_manual_market_order(self, **kw):
        self.calls.append(("open_manual_market_order", kw))
        if self._order_error:
            raise self._order_error
        return {"trade_id": "T1", "ok": True}

    async def open_trade(self, **kw):
        self.calls.append(("open_trade", kw))
        if self._order_error:
            raise self._order_error
        return {"trade_id": "T1", "ok": True}

    async def get_fresh_tick(self):
        raise RuntimeError("no tick in tests")

    async def background_open_commentary(self, *a, **kw):
        raise AssertionError("commentary must not run in tests")


@pytest.fixture
def server():
    return SyncServer()


@pytest.fixture
def mirrored(monkeypatch):
    """Records mirror_insert_signal_if_absent calls, in order with the trade."""
    from backend.src.services.cluster import sync_repo
    seen = []
    monkeypatch.setattr(sync_repo, "mirror_insert_signal_if_absent",
                        lambda sid, kw: seen.append(sid))
    return seen


def _signal_msg(**over):
    m = {"signal_id": "SIG-1", "direction": "BUY", "entry_low": 4500.0,
         "entry_high": 4502.0, "stop_loss": 4495.0, "tp1": 4505.0,
         "lot_size": 0.05, "strategy": "scalp", "tg_source": "Gold Diggers VIP"}
    m.update(over)
    return m


class TestNoEngineOnThisNode:
    async def test_a_market_order_is_refused_with_an_ack(self, server):
        """Refusing silently is the failure. The Mac's Market Order button
        waits on this ack."""
        ws = _Ws()
        await server._handle_market_order(ws, {"direction": "BUY"})

        assert ws.sent[0]["type"] == MSG_MARKET_ORDER_ACK
        assert ws.sent[0]["error"]

    async def test_a_signal_order_is_refused_with_an_ack(self, server):
        ws = _Ws()
        await server._handle_signal_order(ws, _signal_msg())

        assert ws.sent[0]["type"] == MSG_SIGNAL_ORDER_ACK
        assert ws.sent[0]["error"]

    async def test_nothing_is_mirrored_when_there_is_no_engine(self, server, mirrored):
        """The mirror row is only there to satisfy the trade's foreign key.
        Writing one for a trade that cannot happen leaves an orphan signal."""
        await server._handle_signal_order(_Ws(), _signal_msg())
        assert mirrored == []


class TestForwardedSignalOrders:
    async def test_the_signal_is_MIRRORED_BEFORE_the_trade_is_opened(
            self, server, monkeypatch):
        """THE regression. vantage_simulated_trades has a foreign key to
        vantage_signals, and the signal_id came from the Mac's table. Mirror
        after, or not at all, and every forwarded trade fails the constraint
        -- which is exactly what happened for hours after centralized mode was
        switched on."""
        from backend.src.services.cluster import sync_repo
        order: list[str] = []
        monkeypatch.setattr(sync_repo, "mirror_insert_signal_if_absent",
                            lambda sid, kw: order.append("mirror"))
        engine = _FakeEngine()
        server._main_engine = engine

        async def _open(**kw):
            order.append("open_trade")
            return {"trade_id": "T1"}
        engine.open_trade = _open

        await server._handle_signal_order(_Ws(), _signal_msg())

        assert order == ["mirror", "open_trade"], \
            "the signal was not mirrored before the trade was opened"

    async def test_it_mirrors_the_MACS_signal_id(self, server, mirrored):
        server._main_engine = _FakeEngine()
        await server._handle_signal_order(_Ws(), _signal_msg(signal_id="SIG-42"))
        assert mirrored == ["SIG-42"]

    async def test_every_level_is_forwarded_untouched(self, server, mirrored):
        """The Mac has already resolved risk sizing, session gates and SL/TP.
        This node must not re-derive or drop any of it."""
        engine = _FakeEngine()
        server._main_engine = engine
        msg = _signal_msg(tp2=4506.0, tp3=4507.0)

        await server._handle_signal_order(_Ws(), msg)

        _, kw = engine.calls[0]
        assert kw["direction"] == "BUY"
        assert kw["entry_low"] == 4500.0 and kw["entry_high"] == 4502.0
        assert kw["stop_loss"] == 4495.0
        assert kw["tp1"] == 4505.0 and kw["tp2"] == 4506.0 and kw["tp3"] == 4507.0
        assert kw["lot_size"] == 0.05
        assert kw["strategy"] == "scalp"
        assert kw["tg_source"] == "Gold Diggers VIP"

    async def test_a_missing_lot_size_falls_back_to_the_minimum(self, server, mirrored):
        """0.01, not 0 and not None. A zero lot would be rejected by the
        broker; a None would raise inside the engine."""
        engine = _FakeEngine()
        server._main_engine = engine
        msg = _signal_msg()
        del msg["lot_size"]

        await server._handle_signal_order(_Ws(), msg)

        assert engine.calls[0][1]["lot_size"] == 0.01

    async def test_the_ack_carries_the_result(self, server, mirrored):
        server._main_engine = _FakeEngine()
        ws = _Ws()

        await server._handle_signal_order(ws, _signal_msg())

        assert ws.sent[0]["result"] == {"trade_id": "T1", "ok": True}

    async def test_A_FAILED_TRADE_STILL_ACKS_WITH_THE_REASON(self, server, mirrored):
        """The silent-failure bug. Without an ack the Mac cannot tell a
        rejected trade from a lost connection, and the user sees nothing."""
        server._main_engine = _FakeEngine(order_error=RuntimeError("no money"))
        ws = _Ws()

        await server._handle_signal_order(ws, _signal_msg())

        assert ws.sent[0]["type"] == MSG_SIGNAL_ORDER_ACK
        assert "no money" in ws.sent[0]["error"]
        assert "result" not in ws.sent[0]

    async def test_the_ack_is_sent_BEFORE_commentary_is_attempted(self, server, mirrored):
        """Commentary is best-effort AI narration. The fake raises from
        get_fresh_tick and asserts if commentary itself runs, so this passing
        proves the ack does not depend on either."""
        server._main_engine = _FakeEngine()
        ws = _Ws()

        await server._handle_signal_order(ws, _signal_msg())

        assert ws.sent[0].get("result") == {"trade_id": "T1", "ok": True}


class TestForwardedMarketOrders:
    async def test_it_forwards_the_order(self, server):
        engine = _FakeEngine()
        server._main_engine = engine

        await server._handle_market_order(_Ws(), {
            "direction": "SELL", "stop_loss": 4510.0,
            "lot_size": 0.02, "strategy": "scalp"})

        name, kw = engine.calls[0]
        assert name == "open_manual_market_order"
        assert kw["direction"] == "SELL"
        assert kw["stop_loss"] == 4510.0
        assert kw["lot_size"] == 0.02

    async def test_an_ABSENT_take_profit_is_not_passed_at_all(self, server):
        """Passing take_profit=None would override the engine's own default
        rather than leaving it alone -- which is why it is conditional."""
        engine = _FakeEngine()
        server._main_engine = engine

        await server._handle_market_order(_Ws(), {"direction": "BUY"})

        assert "take_profit" not in engine.calls[0][1]

    async def test_a_present_take_profit_IS_passed(self, server):
        engine = _FakeEngine()
        server._main_engine = engine

        await server._handle_market_order(_Ws(), {
            "direction": "BUY", "take_profit": 4520.0})

        assert engine.calls[0][1]["take_profit"] == 4520.0

    async def test_source_name_is_conditional_the_same_way(self, server):
        engine = _FakeEngine()
        server._main_engine = engine

        await server._handle_market_order(_Ws(), {"direction": "BUY"})
        assert "source_name" not in engine.calls[0][1]

        await server._handle_market_order(_Ws(), {
            "direction": "BUY", "source_name": "Manual"})
        assert engine.calls[1][1]["source_name"] == "Manual"

    async def test_a_failed_order_acks_with_the_reason(self, server):
        server._main_engine = _FakeEngine(order_error=RuntimeError("market closed"))
        ws = _Ws()

        await server._handle_market_order(ws, {"direction": "BUY"})

        assert "market closed" in ws.sent[0]["error"]

    async def test_exactly_one_ack_is_sent_per_order(self, server):
        """Two acks would resolve the Mac's pending future twice."""
        server._main_engine = _FakeEngine()
        ws = _Ws()

        await server._handle_market_order(ws, {"direction": "BUY"})

        assert len(ws.sent) == 1
