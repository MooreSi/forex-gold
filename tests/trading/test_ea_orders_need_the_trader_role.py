"""The EA refuses to open anything on a node that is not the active trader.

2026-09-28, on the owner's demo account: the Mac was stood down (the VPS was
the active trader) and still placed a real BUY LIMIT, because the Limit Runner
hands its order straight to `EABridge.place_pending_order` and never passes
through `open_trade()`, where the stand-down gate lives. tg_id=31099 went to
the Mac's EA, filled at 18:06 as ticket 2104195879 and closed at TP with
neither node tracking it. Entry Realignment has the same shape through
`EABridge.open_trade`.

So the gate sits on the two EA calls that open exposure -- the funnel every
route shares -- rather than on each caller (docs/system/rules/20-trading-
safety.md, "Gate the funnel, not the callers").

Nothing here reaches a broker. The bridge's writer is a fake that records
bytes; no MT5 terminal, EA or network socket exists in these tests.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from backend.src.services.broker import ea_bridge
from backend.src.services.trading import limit_order_signal as los


class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


def _healthy_bridge():
    bridge = ea_bridge.EABridge(engine=None)
    bridge._writer = _FakeWriter()
    bridge._last_seen = time.time()
    return bridge


_ROLE = "backend.src.services.cluster.node_roles.is_active_trader_node"


def _place_pending(bridge, timeout=0.05):
    return bridge.place_pending_order(
        "trade-1", "BUY", 4127.0, 0.02, 4118.0, {1: 4131.0}, [1.0], 0,
        "limit_runner", timeout=timeout,
    )


def _open(bridge, timeout=0.05):
    return bridge.open_trade(
        "trade-2", "BUY", 0.02, 4118.0, {1: 4131.0}, "limit_runner", timeout=timeout,
    )


class TestStoodDownNodeSendsNothing:
    def test_pending_order_is_refused(self):
        bridge = _healthy_bridge()
        with patch(_ROLE, return_value=False):
            with pytest.raises(Exception) as exc:
                asyncio.run(_place_pending(bridge))
        assert "stood down" in str(exc.value).lower()
        assert bridge._writer.written == []

    def test_market_open_is_refused(self):
        bridge = _healthy_bridge()
        with patch(_ROLE, return_value=False):
            with pytest.raises(Exception) as exc:
                asyncio.run(_open(bridge))
        assert "stood down" in str(exc.value).lower()
        assert bridge._writer.written == []


class TestActiveTraderStillSends:
    """The gate must not stop the node that is supposed to trade."""

    def test_pending_order_goes_out(self):
        bridge = _healthy_bridge()
        with patch(_ROLE, return_value=True):
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(_place_pending(bridge))
        assert len(bridge._writer.written) == 1

    def test_market_open_goes_out(self):
        bridge = _healthy_bridge()
        with patch(_ROLE, return_value=True):
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(_open(bridge))
        assert len(bridge._writer.written) == 1

    def test_a_role_check_that_errors_fails_open(self):
        """node_roles promises fail-open for standalone installs; an error in
        the check must not silently stop a standalone node trading."""
        bridge = _healthy_bridge()
        with patch(_ROLE, side_effect=RuntimeError("no database")):
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(_place_pending(bridge))
        assert len(bridge._writer.written) == 1


class _StoodDownEA:
    """What the Limit Runner sees from the real bridge on a stood-down node."""

    def __init__(self):
        self.calls = 0

    def is_ea_healthy(self):
        return True

    async def place_pending_order(self, *a, **kw):
        self.calls += 1
        raise ea_bridge.StoodDownError(
            "Trading stood down — the VPS is the active trader")


async def _balance():
    return 1000.0


def _lot_size(entry, sl, balance, risk_pct):
    return 0.10


def _parsed():
    return {
        "direction": "BUY", "entry_low": 4122.0, "entry_high": 4127.0,
        "stop_loss": 4118.0, "tp1": 4131.0, "tp2": 4135.0, "tp3": None,
        "tp4": None, "tp5": None, "tp6": None, "tp7": None, "tp8": None,
        "tp_open": False,
    }


@pytest.mark.asyncio
async def test_limit_runner_reports_a_deferral_not_a_failure(fresh_db):
    """The Mac parses every limit signal the VPS does. Each one must read as
    'the other node has it', not raise a 'Limit order failed' alert."""
    ea = _StoodDownEA()
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=ea):
        result = await los.handle_limit_order_signal(
            _parsed(), "tg1", "chan", "chan",
            {"risk_per_trade_pct": 0.5, "strategy_lot_size": 0},
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert ea.calls == 1
    assert "failed" not in result["skip_reason"].lower()
    assert "active node" in result["skip_reason"].lower()
