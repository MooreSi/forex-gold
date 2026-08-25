"""Characterizes SimulationEngine.build_orb_report/_orb_auto_execute
(core/engine.py) -- thin delegates to core_orb_report.py, which
test_orb_report_surface.py exercises directly. This file proves the
SimulationEngine wrapper methods still delegate correctly after the
2026-08-01 ORB methodology rebuild (whole Asian session as a confirmation
filter, first 15 minutes of London as the traded opening range -- see
core_orb_report.py's module docstring).

datetime.now(timezone.utc) is controlled via
unittest.mock.patch("backend.src.services.analytics.orb_report.datetime") (the
module the logic now actually lives in). No real or demo MT5 order is
ever placed -- build_orb_report tests only call get_tick/get_candles_range,
and _orb_auto_execute tests mock core_manual_market_order.
open_manual_market_order (a separately tested collaborator).
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.runtime import SimulationEngine
from backend.src.runtime import TradingRuntime


def _patched_now(fixed_dt):
    patcher = mock.patch("backend.src.services.analytics.orb_report.datetime")
    mock_dt = patcher.start()
    mock_dt.now.return_value = fixed_dt
    mock_dt.fromtimestamp.side_effect = lambda *a, **kw: datetime.fromtimestamp(*a, **kw)
    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
    return patcher


# Monday 2026-07-20, BST in effect -- London's 08:00 local open is 07:00 UTC.
_ASIA_START_TS = datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc).timestamp()
_OR_START_TS   = datetime(2026, 7, 20, 7, 0, 0, tzinfo=timezone.utc).timestamp()
_OR_END_TS     = _OR_START_TS + 15 * 60
_ACTIVE_NOW    = datetime(2026, 7, 20, 9, 0, 0, tzinfo=timezone.utc)
_BEFORE_OPEN   = datetime(2026, 7, 20, 6, 0, 0, tzinfo=timezone.utc)


def _standard_candles():
    candles = []
    t = _ASIA_START_TS
    while t < _ASIA_START_TS + 8 * 3600:
        if _OR_START_TS <= t < _OR_END_TS:
            high, low = 2402.0, 2398.0
        else:
            high, low = 2405.0, 2395.0
        candles.append({"ts": t, "high": high, "low": low, "volume": 5.0})
        t += 60
    return candles


class _FakeBridge:
    def __init__(self, candles=None, tick=None):
        self._candles = candles if candles is not None else _standard_candles()
        self._tick = tick or SimpleNamespace(bid=2400.0, ask=2400.5)

    async def get_tick(self):
        return self._tick

    async def get_candles_range(self, start, end, timeframe="M1"):
        return [c for c in self._candles if start <= c["ts"] < end]


# ── build_orb_report ─────────────────────────────────────────────────────────

def test_no_tick_returns_none(fresh_db):
    class _NoTickBridge(_FakeBridge):
        async def get_tick(self):
            return None
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _NoTickBridge()
    p = _patched_now(_ACTIVE_NOW)
    try:
        assert asyncio.run(SimulationEngine.build_orb_report(e)) is None
    finally:
        p.stop()


def test_before_london_open_returns_none(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge()
    p = _patched_now(_BEFORE_OPEN)
    try:
        assert asyncio.run(TradingRuntime.build_orb_report(e)) is None
    finally:
        p.stop()


def test_bullish_breakout_confirmed_beyond_both_ranges(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge(tick=SimpleNamespace(bid=2409.5, ask=2410.0))
    p = _patched_now(_ACTIVE_NOW)
    try:
        report = asyncio.run(TradingRuntime.build_orb_report(e))
    finally:
        p.stop()
    assert report["direction"] == "bullish"
    assert report["or_high"] == 2402.0
    assert report["asia_high"] == 2405.0
    assert report["stop"] == 2400.0
    assert report["target"] == 2406.0
    assert report["target2"] == 2408.0
    assert report["rr"] == 2.0


def test_bearish_breakout_confirmed_beyond_both_ranges(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge(tick=SimpleNamespace(bid=2389.5, ask=2390.0))
    p = _patched_now(_ACTIVE_NOW)
    try:
        report = asyncio.run(SimulationEngine.build_orb_report(e))
    finally:
        p.stop()
    assert report["direction"] == "bearish"
    assert report["stop"] == 2400.0
    assert report["target"] == 2394.0
    assert report["target2"] == 2392.0
    assert report["rr"] == 2.0


def test_breaks_opening_range_but_inside_asia_is_unconfirmed(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge(tick=SimpleNamespace(bid=2403.0, ask=2403.5))
    p = _patched_now(_ACTIVE_NOW)
    try:
        report = asyncio.run(SimulationEngine.build_orb_report(e))
    finally:
        p.stop()
    assert report["direction"] == "unconfirmed"
    assert report["stop"] is None


# ── _orb_auto_execute ────────────────────────────────────────────────────────
# Places a genuine immediate MARKET order via core_manual_market_order.
# open_manual_market_order (2026-08-01) -- mocked as a collaborator
# boundary, separately tested in its own module's tests.

_BULLISH_REPORT = {"direction": "bullish", "stop": 2400.0, "target": 2406.0, "target2": 2408.0}


def _patch_open_market(result=None, side_effect=None):
    m = mock.AsyncMock(
        return_value=result if result is not None else {"entry_price": 2410.0, "mt5_ticket": 777},
        side_effect=side_effect,
    )
    return mock.patch("backend.src.services.trading.manual_market_order.open_manual_market_order", new=m), m


def test_auto_execute_not_proceeding_does_not_place_order(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    patcher, m = _patch_open_market()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=False), patcher:
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))
    m.assert_not_called()


def test_auto_execute_direction_inside_does_not_place_order(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    patcher, m = _patch_open_market()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), patcher:
        asyncio.run(SimulationEngine._orb_auto_execute(e, {"direction": "inside"}))
    m.assert_not_called()


def test_auto_execute_bullish_places_market_order(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    patcher, m = _patch_open_market()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), patcher:
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))
    m.assert_called_once()
    args, kwargs = m.call_args
    assert args[1] == "BUY"
    assert kwargs["stop_loss"] == 2400.0
    assert kwargs["take_profit"] == 2406.0
    assert kwargs["strategy"] == "orb_fixed"


def test_auto_execute_uses_orb_lot_size_risk_setting(fresh_db):
    rs = db.get_risk_settings()
    rs["orb_lot_size"] = 0.05
    db.update_risk_settings(rs)
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    patcher, m = _patch_open_market()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), patcher:
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))
    assert m.call_args.kwargs["lot_size"] == 0.05


def test_auto_execute_market_order_failure_does_not_raise(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    patcher, m = _patch_open_market(side_effect=ConnectionError("EA send failed"))
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), patcher, \
         mock.patch("backend.src.services.telegram.alerts.send_message"):
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))  # must not raise
    m.assert_called_once()
