"""Proves forex_trader.core.core_orb_report's extracted functions behave
identically to SimulationEngine's originals, characterized in
test_orb_report_characterization.py -- see
docs/todo/refactor/core-orb-report-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. No real or demo MT5 order is ever placed, closed, or modified --
this cluster never calls a bridge order method at all.
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
    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
    return patcher


# London opens 07:00 UTC (BST) that day; the reference range is now the
# last _ORB_RANGE_HOURS (1h) before that, not the full Asian session.
_LONDON_OPEN_TS = datetime(2026, 7, 20, 7, 0, 0, tzinfo=timezone.utc).timestamp()
_ASIA_START_TS = _LONDON_OPEN_TS - 3600
_FIXED_NOW = datetime(2026, 7, 20, 7, 5, 0, tzinfo=timezone.utc)  # 5 min into the 15-min window


def _standard_candles():
    # Same shape (wide/low-volume - tight/high-volume - wide/low-volume) as
    # the original 8h fixture, proportionally compressed into the new 1h
    # window -- produces identical range/poc/vah/val numbers.
    candles = []
    for i in range(0, 60, 1):
        ts = _ASIA_START_TS + i * 60
        if 15 <= i < 45:
            high, low, vol = 2402.0, 2398.0, 50.0
        else:
            high, low, vol = (2410.0, 2390.0, 5.0) if i < 15 else (2405.0, 2395.0, 5.0)
        candles.append({"ts": ts, "high": high, "low": low, "volume": vol})
    return candles


class _FakeBridge:
    def __init__(self, candles=None, tick=None):
        self._candles = candles if candles is not None else _standard_candles()
        self._tick = tick or SimpleNamespace(bid=2414.5, ask=2415.0)

    async def get_tick(self):
        return self._tick

    async def get_candles_range(self, start, end, timeframe="M1"):
        return [c for c in self._candles if start <= c["ts"] < end]


# ── build_orb_report ─────────────────────────────────────────────────────────

def test_no_tick_returns_none(fresh_db):
    class _NoTickBridge(_FakeBridge):
        async def get_tick(self):
            return None
    assert asyncio.run(orb.build_orb_report(_NoTickBridge())) is None


def test_before_london_open_returns_none(fresh_db):
    early_now = datetime(2026, 7, 20, 6, 0, 0, tzinfo=timezone.utc)
    p = _patched_now(early_now)
    try:
        assert asyncio.run(orb.build_orb_report(_FakeBridge())) is None
    finally:
        p.stop()


def test_after_london_window_closes_returns_none(fresh_db):
    late_now = datetime(2026, 7, 20, 7, 20, 0, tzinfo=timezone.utc)  # 20 min after open — past the 15-min window
    p = _patched_now(late_now)
    try:
        assert asyncio.run(orb.build_orb_report(_FakeBridge())) is None
    finally:
        p.stop()


def test_no_candles_returns_none(fresh_db):
    class _NoCandlesBridge(_FakeBridge):
        async def get_candles_range(self, start, end, timeframe="M1"):
            return []
    p = _patched_now(_FIXED_NOW)
    try:
        assert asyncio.run(orb.build_orb_report(_NoCandlesBridge())) is None
    finally:
        p.stop()


def test_price_inside_range_reports_inside_direction(fresh_db):
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2399.5, ask=2400.0))
    p = _patched_now(_FIXED_NOW)
    try:
        report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()
    assert report["direction"] == "inside"
    assert report["entry_zone_low"] is None
    assert report["stop"] is None
    assert report["target"] is None
    assert "10.0 pts above the Low" in report["position_note"]


def test_bullish_breakout_computes_zone_stop_target(fresh_db):
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2414.5, ask=2415.0))
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch.object(orb, "get_orb_target_multiple",
                               new=mock.AsyncMock(return_value={"multiple": 2.0, "n": 10, "is_default": False})):
            report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()

    assert report["direction"] == "bullish"
    assert report["range_high"] == 2410.0
    assert report["range_low"] == 2390.0
    assert report["poc"] == 2398.25
    assert report["vah"] == 2401.5
    assert report["val"] == 2398.0
    assert report["stop"] == 2400.0
    assert report["target"] == 2450.0
    assert report["entry_zone_low"] == 2406.0
    assert report["entry_zone_high"] == 2406.0
    assert report["rr"] == 7.33


def test_bearish_breakout_computes_mirrored_zone_stop_target(fresh_db):
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2385.0, ask=2385.5))
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch.object(orb, "get_orb_target_multiple",
                               new=mock.AsyncMock(return_value={"multiple": 2.0, "n": 10, "is_default": False})):
            report = asyncio.run(orb.build_orb_report(bridge))
    finally:
        p.stop()

    assert report["direction"] == "bearish"
    assert report["stop"] == 2400.0
    assert report["target"] == 2350.0
    assert report["entry_zone_low"] == 2394.0
    assert report["entry_zone_high"] == 2394.0
    assert report["rr"] == 7.33


# ── get_orb_target_multiple ─────────────────────────────────────────────────

def test_get_target_multiple_uses_cache_when_dated_today(fresh_db):
    db.set_app_config("orb_target_multiple_date", "2026-07-20")
    db.set_app_config("orb_target_multiple", "1.8")
    db.set_app_config("orb_target_multiple_n", "15")
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch.object(orb, "backtest_orb_target_multiple", new=mock.AsyncMock()) as bt:
            result = asyncio.run(orb.get_orb_target_multiple(_FakeBridge()))
    finally:
        p.stop()
    assert result == {"multiple": 1.8, "n": 15, "is_default": False}
    bt.assert_not_called()


def test_get_target_multiple_recomputes_when_cache_stale(fresh_db):
    db.set_app_config("orb_target_multiple_date", "2026-07-01")
    db.set_app_config("orb_target_multiple", "1.8")
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch.object(
            orb, "backtest_orb_target_multiple",
            new=mock.AsyncMock(return_value={"multiple": 2.2, "n": 12, "is_default": False}),
        ) as bt:
            result = asyncio.run(orb.get_orb_target_multiple(_FakeBridge()))
    finally:
        p.stop()
    assert result == {"multiple": 2.2, "n": 12, "is_default": False}
    bt.assert_called_once()
    assert db.get_app_config("orb_target_multiple_date") == "2026-07-20"
    assert db.get_app_config("orb_target_multiple") == "2.2"


# ── backtest_orb_target_multiple ────────────────────────────────────────────

class _ShapedBackfillBridge:
    def __init__(self, shape):
        self.shape = shape

    async def get_candles_range(self, start, end, timeframe="M1"):
        candles = []
        t = start
        while t < end:
            rel = t - start
            if rel < 8 * 3600:
                candles.append({"ts": t, "high": 2410.0, "low": 2390.0, "volume": 5.0})
            elif self.shape == "clean_bull":
                candles.append({"ts": t, "high": 2440.0, "low": 2405.0, "volume": 5.0})
            elif self.shape == "ambiguous":
                candles.append({"ts": t, "high": 2440.0, "low": 2360.0, "volume": 5.0})
            t += 300
        return candles


def test_backtest_enough_clean_samples_returns_median(fresh_db):
    monday = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)
    p = _patched_now(monday)
    try:
        result = asyncio.run(orb.backtest_orb_target_multiple(_ShapedBackfillBridge("clean_bull")))
    finally:
        p.stop()
    assert result == {"multiple": 1.5, "n": 17, "is_default": False}


def test_backtest_no_clean_breakouts_falls_back_to_default(fresh_db):
    monday = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)
    p = _patched_now(monday)
    try:
        result = asyncio.run(orb.backtest_orb_target_multiple(_ShapedBackfillBridge("ambiguous")))
    finally:
        p.stop()
    assert result == {"multiple": 2.0, "n": 0, "is_default": True}


# ── orb_auto_execute ─────────────────────────────────────────────────────────
# Places a genuine EA pending limit order (2026-07-22) -- no Python-bridge
# fallback, same convention as Limit Runner (core_limit_order_signal.py).

_BULLISH_REPORT = {
    "direction": "bullish", "entry_zone_low": 2406.0, "entry_zone_high": 2408.0,
    "stop": 2400.0, "target": 2450.0,
}


class _FakeEA:
    def __init__(self, healthy=True, ack=None):
        self._healthy = healthy
        self._ack = ack if ack is not None else {"type": "pending_order_placed", "ticket": 777}
        self.calls: list[dict] = []

    def is_ea_healthy(self):
        return self._healthy

    async def place_pending_order(self, trade_id, direction, price, lot_size, stop_loss,
                                  tps, pcts, be_at_pos, strategy, expire_minutes=240.0,
                                  close_full_on_last=True):
        self.calls.append(dict(
            trade_id=trade_id, direction=direction, price=price, lot_size=lot_size,
            stop_loss=stop_loss, tps=dict(tps), pcts=list(pcts), be_at_pos=be_at_pos,
            strategy=strategy, expire_minutes=expire_minutes,
            close_full_on_last=close_full_on_last,
        ))
        return self._ack


def test_auto_execute_not_proceeding_creates_no_signal(fresh_db):
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), False))
    assert fake_ea.calls == []
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


def test_auto_execute_direction_inside_creates_no_signal(fresh_db):
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute({"direction": "inside"}, _FakeBridge(), True))
    assert fake_ea.calls == []
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


def test_auto_execute_ea_unhealthy_creates_no_signal(fresh_db):
    """No Python-bridge fallback -- an unavailable EA means the setup is
    simply not captured, not simulated some other way."""
    fake_ea = _FakeEA(healthy=False)
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert fake_ea.calls == []
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


def test_auto_execute_bullish_places_pending_order_at_near_edge(fresh_db):
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))

    assert len(fake_ea.calls) == 1
    call = fake_ea.calls[0]
    assert call["direction"] == "BUY"
    assert call["price"] == 2408.0  # entry_zone_high -- near edge for a BUY
    assert call["stop_loss"] == 2400.0
    assert call["tps"] == {1: 2450.0}
    assert call["strategy"] == "orb_fixed"
    assert call["close_full_on_last"] is True

    with db.db() as conn:
        sig = conn.execute(
            "SELECT source_name, direction, entry_low, entry_high, stop_loss, tp1 "
            "FROM vantage_signals"
        ).fetchone()
        assert tuple(sig) == ("ORB/IVB Report (auto)", "BUY", 2406.0, 2408.0, 2400.0, 2450.0)

        po = conn.execute(
            "SELECT direction, price, stop_loss, ea_ticket, status, strategy "
            "FROM vantage_pending_orders"
        ).fetchone()
        assert tuple(po) == ("BUY", 2408.0, 2400.0, 777, "working", "orb_fixed")


def test_auto_execute_bearish_uses_entry_zone_low_as_price(fresh_db):
    bearish_report = {
        "direction": "bearish", "entry_zone_low": 2392.0, "entry_zone_high": 2394.0,
        "stop": 2400.0, "target": 2350.0,
    }
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(bearish_report, _FakeBridge(), True))
    assert fake_ea.calls[0]["direction"] == "SELL"
    assert fake_ea.calls[0]["price"] == 2392.0  # entry_zone_low -- near edge for a SELL


def test_auto_execute_uses_orb_lot_size_risk_setting(fresh_db):
    rs = db.get_risk_settings()
    rs["orb_lot_size"] = 0.05
    db.update_risk_settings(rs)
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert fake_ea.calls[0]["lot_size"] == 0.05
    with db.db() as conn:
        lot = conn.execute("SELECT lot_size FROM vantage_signals").fetchone()[0]
    assert lot == 0.05


def test_auto_execute_place_pending_order_failure_does_not_raise(fresh_db):
    class _RaisingEA(_FakeEA):
        async def place_pending_order(self, *a, **kw):
            raise ConnectionError("EA send failed")
    fake_ea = _RaisingEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))  # must not raise
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


def test_auto_execute_ea_rejection_does_not_write_db_rows(fresh_db):
    fake_ea = _FakeEA(ack={"type": "pending_order_open_failed", "error": "Invalid stops"})
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


# ── Channel Strategy override (2026-07-23: ORB/IVB Report is now a regular
# canonical channel -- Trading > Strategy > Channel Strategy) ────────────────

def test_auto_execute_respects_channel_strategy_override(fresh_db):
    db.set_channel_strategy_override("ORB/IVB Report (auto)", "trend_ratchet")
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert fake_ea.calls[0]["strategy"] == "trend_ratchet"
    with db.db() as conn:
        strat = conn.execute("SELECT strategy FROM vantage_pending_orders").fetchone()[0]
    assert strat == "trend_ratchet"


def test_auto_execute_auto_mode_uses_channel_strategy_rec(fresh_db):
    db.set_channel_strategy_override("ORB/IVB Report (auto)", None, auto=True)
    db.set_channel_strategy_rec("ORB/IVB Report (auto)", "breakeven_runner", "trending", 0.8)
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert fake_ea.calls[0]["strategy"] == "breakeven_runner"


def test_auto_execute_no_override_still_defaults_to_orb_fixed(fresh_db):
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert fake_ea.calls[0]["strategy"] == "orb_fixed"


def test_orb_report_is_a_canonical_channel(fresh_db):
    from forex_trader.core.core_db_channel import CANONICAL_CHANNEL_ORDER, _canonical
    assert "ORB/IVB Report" in CANONICAL_CHANNEL_ORDER
    assert _canonical("ORB/IVB Report (auto)") == "ORB/IVB Report"


def test_auto_execute_ea_template_override_skips_with_no_ea_call(fresh_db):
    """EA Templates manage immediate-fill trades end-to-end -- they don't fit
    ORB/IVB's pending zone-entry order, so this must skip cleanly rather
    than silently placing a template-tagged pending order the EA's grid/
    single-mode dispatch was never built to receive."""
    from forex_trader.core import core_ea_templates as ea_templates
    ea_templates.save_ea_template("Scalp Grid", {"mode": "grid"})
    db.set_channel_strategy_override(
        "ORB/IVB Report (auto)", ea_templates.override_for_template("Scalp Grid"),
    )
    fake_ea = _FakeEA()
    with mock.patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        with mock.patch("forex_trader.core.telegram_alerts.send_message"):
            asyncio.run(orb.orb_auto_execute(_BULLISH_REPORT, _FakeBridge(), True))
    assert fake_ea.calls == []
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0
