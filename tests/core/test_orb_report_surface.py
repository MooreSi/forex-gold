"""Tests forex_trader.core.core_orb_report's ORB methodology rebuilt
2026-08-01: whole Asian session (00:00-08:00 UTC) as a confirmation
filter, the first 15 minutes of London (08:00-08:15 UTC local, DST-
adjusted) as the traded opening range, stop at the opening range's
midpoint, target at 2x the resulting risk. See core_orb_report.py's
module docstring for the full rationale.

No real or demo MT5 order is ever placed in build_orb_report tests -- it
only calls get_tick/get_candles_range. orb_auto_execute tests mock
core_manual_market_order.open_manual_market_order (a separately tested
collaborator) rather than exercising real order placement.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_orb_report as orb


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def _patched_now(fixed_dt):
    patcher = mock.patch("forex_trader.core.core_orb_report.datetime")
    mock_dt = patcher.start()
    mock_dt.now.return_value = fixed_dt
    mock_dt.fromtimestamp.side_effect = lambda *a, **kw: datetime.fromtimestamp(*a, **kw)
    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
    return patcher


# Monday 2026-07-20, BST in effect -- London's 08:00 local open is 07:00 UTC.
_ASIA_START_TS = datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc).timestamp()
_OR_START_TS   = datetime(2026, 7, 20, 7, 0, 0, tzinfo=timezone.utc).timestamp()
_OR_END_TS     = _OR_START_TS + 15 * 60
_FORMING_NOW   = datetime(2026, 7, 20, 7, 5, 0, tzinfo=timezone.utc)   # inside the OR window
_ACTIVE_NOW    = datetime(2026, 7, 20, 9, 0, 0, tzinfo=timezone.utc)   # well after the OR closes
_BEFORE_OPEN   = datetime(2026, 7, 20, 6, 0, 0, tzinfo=timezone.utc)

# Overnight band 2395-2405, a tighter opening-range band 2398-2402 for the
# 07:00-07:15 window -- asia_high=2405, asia_low=2395, or_high=2402, or_low=2398.
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
    p = _patched_now(_ACTIVE_NOW)
    try:
        assert asyncio.run(orb.build_orb_report(_NoTickBridge())) is None
    finally:
        p.stop()


def test_before_london_open_returns_none(fresh_db):
    p = _patched_now(_BEFORE_OPEN)
    try:
        assert asyncio.run(orb.build_orb_report(_FakeBridge())) is None
    finally:
        p.stop()


def test_no_asia_candles_returns_none(fresh_db):
    class _NoCandlesBridge(_FakeBridge):
        async def get_candles_range(self, start, end, timeframe="M1"):
            return []
    p = _patched_now(_ACTIVE_NOW)
    try:
        assert asyncio.run(orb.build_orb_report(_NoCandlesBridge())) is None
    finally:
        p.stop()


def test_forming_phase_during_opening_range_window(fresh_db):
    bridge = _FakeBridge()
    p = _patched_now(_FORMING_NOW)
    try:
        report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()
    assert report["phase"] == "forming"
    assert report["direction"] == "inside"
    assert report["asia_high"] == 2405.0
    assert report["asia_low"] == 2395.0
    assert "still forming" in report["position_note"]


def test_no_or_candles_returns_none(fresh_db):
    class _AsiaOnlyBridge(_FakeBridge):
        async def get_candles_range(self, start, end, timeframe="M1"):
            if start >= _OR_START_TS and end <= _OR_END_TS:
                return []
            return await super().get_candles_range(start, end, timeframe)
    p = _patched_now(_ACTIVE_NOW)
    try:
        assert asyncio.run(orb.build_orb_report(_AsiaOnlyBridge())) is None
    finally:
        p.stop()


def test_price_inside_opening_range_reports_inside(fresh_db):
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2399.5, ask=2400.0))
    p = _patched_now(_ACTIVE_NOW)
    try:
        report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()
    assert report["direction"] == "inside"
    assert report["stop"] is None
    assert report["target"] is None
    assert report["or_high"] == 2402.0
    assert report["or_low"] == 2398.0


def test_breaks_opening_range_but_inside_asia_is_unconfirmed(fresh_db):
    # 2403.5 clears or_high (2402) but not asia_high (2405).
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2403.0, ask=2403.5))
    p = _patched_now(_ACTIVE_NOW)
    try:
        report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()
    assert report["direction"] == "unconfirmed"
    assert report["stop"] is None
    assert report["target"] is None
    assert "unconfirmed" in report["position_note"] or "still inside the Asian range" in report["position_note"]


def test_bullish_breakout_confirmed_beyond_both_ranges(fresh_db):
    # 2410 clears both or_high (2402) and asia_high (2405).
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2409.5, ask=2410.0))
    p = _patched_now(_ACTIVE_NOW)
    try:
        report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()
    assert report["direction"] == "bullish"
    assert report["or_high"] == 2402.0
    assert report["or_low"] == 2398.0
    assert report["asia_high"] == 2405.0
    assert report["asia_low"] == 2395.0
    # stop = midpoint of the opening range = or_high - 0.5*or_range
    assert report["stop"] == 2400.0
    # target = breakout edge + 2x risk; risk = or_high - stop = 2.0
    assert report["target"] == 2406.0
    assert report["target2"] == 2408.0
    assert report["rr"] == 2.0


def test_bearish_breakout_confirmed_beyond_both_ranges(fresh_db):
    # 2390 clears both or_low (2398) and asia_low (2395).
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2389.5, ask=2390.0))
    p = _patched_now(_ACTIVE_NOW)
    try:
        report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()
    assert report["direction"] == "bearish"
    assert report["stop"] == 2400.0
    assert report["target"] == 2394.0
    assert report["target2"] == 2392.0
    assert report["rr"] == 2.0


# ── orb_auto_execute ─────────────────────────────────────────────────────────
# Places a genuine immediate MARKET order via core_manual_market_order.
# open_manual_market_order (2026-08-01) -- mocked here as a collaborator
# boundary, same way the old pending-limit EA call used to be mocked.

_BULLISH_REPORT = {"direction": "bullish", "stop": 2400.0, "target": 2406.0, "target2": 2408.0}
_BEARISH_REPORT = {"direction": "bearish", "stop": 2400.0, "target": 2394.0, "target2": 2392.0}


def _patch_open_market(result=None, side_effect=None):
    m = mock.AsyncMock(
        return_value=result if result is not None else {"entry_price": 2410.0, "mt5_ticket": 777},
        side_effect=side_effect,
    )
    return mock.patch("forex_trader.core.core_manual_market_order.open_manual_market_order", new=m), m


def test_auto_execute_not_proceeding_does_not_place_order(fresh_db):
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), False))
    m.assert_not_called()


def test_auto_execute_direction_inside_does_not_place_order(fresh_db):
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute({"direction": "inside"}, _FakeBridge(), True))
    m.assert_not_called()


def test_auto_execute_direction_unconfirmed_does_not_place_order(fresh_db):
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute({"direction": "unconfirmed"}, _FakeBridge(), True))
    m.assert_not_called()


def test_auto_execute_missing_stop_or_target_does_not_place_order(fresh_db):
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute({"direction": "bullish", "stop": None, "target": 2406.0},
                                          _FakeBridge(), True))
    m.assert_not_called()


def test_auto_execute_bullish_places_market_order(fresh_db):
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    m.assert_called_once()
    args, kwargs = m.call_args
    assert args[1] == "BUY"
    assert kwargs["stop_loss"] == 2400.0
    assert kwargs["take_profit"] == 2406.0
    assert kwargs["strategy"] == "orb_fixed"
    assert kwargs["source_name"] == "ORB/IVB Report (auto)"


def test_auto_execute_bearish_places_sell_market_order(fresh_db):
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BEARISH_REPORT, _FakeBridge(), True))
    args, kwargs = m.call_args
    assert args[1] == "SELL"
    assert kwargs["take_profit"] == 2394.0


def test_auto_execute_uses_orb_lot_size_risk_setting(fresh_db):
    rs = db.get_risk_settings()
    rs["orb_lot_size"] = 0.05
    db.update_risk_settings(rs)
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert m.call_args.kwargs["lot_size"] == 0.05


def test_auto_execute_zero_lot_size_computes_risk_based_lot(fresh_db):
    # orb_lot_size=0 (unset, the default) used to pass lot_size=None straight
    # through to open_manual_market_order -- whose own fallback ladder tries
    # the unrelated global strategy_lot_size BEFORE ever reaching real
    # risk-based sizing, so "0 = auto-size from Risk %" (the ORB panel's own
    # documented behaviour) never actually happened. orb_auto_execute now
    # computes the risk-based lot itself and passes a concrete value instead
    # of None, so it can't fall through to that unrelated setting. This
    # report has no current_price (unlike a real build_orb_report() result),
    # so entry falls back to the report's own stop -- a degenerate
    # zero-distance risk calc that suggest_lot_size clamps to the 0.01
    # minimum, which is what's asserted here.
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert m.call_args.kwargs["lot_size"] == 0.01


def test_auto_execute_market_order_failure_does_not_raise(fresh_db):
    patcher, m = _patch_open_market(side_effect=ConnectionError("EA send failed"))
    with patcher, mock.patch("forex_trader.core.telegram_alerts.send_message"):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))  # must not raise
    m.assert_called_once()


# ── Channel Strategy override (ORB/IVB Report is a regular canonical
# channel -- Trading > Strategy > Channel Strategy) ──────────────────────────

def test_auto_execute_respects_channel_strategy_override(fresh_db):
    db.set_channel_strategy_override("ORB/IVB Report (auto)", "trend_ratchet")
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert m.call_args.kwargs["strategy"] == "trend_ratchet"


def test_auto_execute_auto_mode_uses_channel_strategy_rec(fresh_db):
    db.set_channel_strategy_override("ORB/IVB Report (auto)", None, auto=True)
    db.set_channel_strategy_rec("ORB/IVB Report (auto)", "breakeven_runner", "trending", 0.8)
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert m.call_args.kwargs["strategy"] == "breakeven_runner"


def test_auto_execute_no_override_still_defaults_to_orb_fixed(fresh_db):
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert m.call_args.kwargs["strategy"] == "orb_fixed"


def test_orb_report_is_a_canonical_channel(fresh_db):
    from forex_trader.core.core_db_channel import _FIXED_ENGINE_CHANNELS, _canonical
    assert "ORB/IVB Report" in _FIXED_ENGINE_CHANNELS
    assert _canonical("ORB/IVB Report (auto)") == "ORB/IVB Report"


def test_auto_execute_ea_template_override_skips_with_no_market_order(fresh_db):
    """EA Templates manage immediate-fill trades end-to-end through the
    normal signal path -- they don't fit ORB's direct market entry, so this
    must skip cleanly rather than opening a template-tagged trade the
    template's own grid/single-mode dispatch was never built to receive."""
    from forex_trader.core import core_ea_templates as ea_templates
    ea_templates.save_ea_template("Scalp Grid", {"mode": "grid"})
    db.set_channel_strategy_override(
        "ORB/IVB Report (auto)", ea_templates.override_for_template("Scalp Grid"),
    )
    patcher, m = _patch_open_market()
    with patcher, mock.patch("forex_trader.core.telegram_alerts.send_message"):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    m.assert_not_called()
