"""Characterizes build_orb_report/_get_orb_target_multiple/
_backtest_orb_target_multiple/_orb_auto_execute on SimulationEngine
(core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-orb-report-migration/010-*.md.

datetime.now(timezone.utc) is controlled via
unittest.mock.patch("backend.src.runtime.datetime"); every expected
value below was traced against unmodified engine.py with concrete
candle/tick fixtures before being written into assertions, given the
calendar-window and volume-profile math involved. No real or demo MT5
order is ever placed, closed, or modified -- this cluster never calls a
bridge order method at all, only get_tick/get_candles_range and (for
_orb_auto_execute) create_signal, which is DB-only.
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
    """Context manager patching analytics orb_report.datetime.now() while
    leaving direct datetime(...) construction working via the real class."""
    patcher = mock.patch("backend.src.services.analytics.orb_report.datetime")
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
    """Range 2390-2410 for the outer bands, heavy volume clustered 2398-2402 --
    same shape as the original 8h fixture, proportionally compressed into the
    new 1h window; produces identical range/poc/vah/val numbers."""
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
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _NoTickBridge()
    assert asyncio.run(SimulationEngine.build_orb_report(e)) is None


def test_before_london_open_returns_none(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    early_now = datetime(2026, 7, 20, 6, 0, 0, tzinfo=timezone.utc)
    p = _patched_now(early_now)
    try:
        assert asyncio.run(SimulationEngine.build_orb_report(e)) is None
    finally:
        p.stop()


def test_after_london_window_closes_returns_none(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    late_now = datetime(2026, 7, 20, 7, 20, 0, tzinfo=timezone.utc)  # 20 min after open — past the 15-min window
    p = _patched_now(late_now)
    try:
        assert asyncio.run(SimulationEngine.build_orb_report(e)) is None
    finally:
        p.stop()


def test_no_candles_returns_none(fresh_db):
    class _NoCandlesBridge(_FakeBridge):
        async def get_candles_range(self, start, end, timeframe="M1"):
            return []
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _NoCandlesBridge()
    p = _patched_now(_FIXED_NOW)
    try:
        assert asyncio.run(SimulationEngine.build_orb_report(e)) is None
    finally:
        p.stop()


def test_price_inside_range_reports_inside_direction(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge(tick=SimpleNamespace(bid=2399.5, ask=2400.0))
    p = _patched_now(_FIXED_NOW)
    try:
        report = asyncio.run(SimulationEngine.build_orb_report(e))
    finally:
        p.stop()
    assert report["direction"] == "inside"
    assert report["entry_zone_low"] is None
    assert report["stop"] is None
    assert report["target"] is None
    assert "10.0 pts above the Low" in report["position_note"]


def test_bullish_breakout_computes_zone_stop_target(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge(tick=SimpleNamespace(bid=2414.5, ask=2415.0))
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch("backend.src.services.analytics.orb_report.get_orb_target_multiple",
                        new=mock.AsyncMock(return_value={"multiple": 2.0, "n": 10, "is_default": False})):
            report = asyncio.run(SimulationEngine.build_orb_report(e))
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
    # min-entry-stop-buffer clamp pulled the raw poc/vah zone up to [2406, 2406]
    assert report["entry_zone_low"] == 2406.0
    assert report["entry_zone_high"] == 2406.0
    assert report["rr"] == 7.33


def test_bearish_breakout_computes_mirrored_zone_stop_target(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge(tick=SimpleNamespace(bid=2385.0, ask=2385.5))
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch("backend.src.services.analytics.orb_report.get_orb_target_multiple",
                        new=mock.AsyncMock(return_value={"multiple": 2.0, "n": 10, "is_default": False})):
            report = asyncio.run(SimulationEngine.build_orb_report(e))
    finally:
        p.stop()

    assert report["direction"] == "bearish"
    assert report["stop"] == 2400.0
    assert report["target"] == 2350.0
    assert report["entry_zone_low"] == 2394.0
    assert report["entry_zone_high"] == 2394.0
    assert report["rr"] == 7.33


# ── _get_orb_target_multiple ────────────────────────────────────────────────

def test_get_target_multiple_uses_cache_when_dated_today(fresh_db):
    db.set_app_config("orb_target_multiple_date", "2026-07-20")
    db.set_app_config("orb_target_multiple", "1.8")
    db.set_app_config("orb_target_multiple_n", "15")
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch("backend.src.services.analytics.orb_report.backtest_orb_target_multiple", new=mock.AsyncMock()) as bt:
            result = asyncio.run(SimulationEngine._get_orb_target_multiple(e))
    finally:
        p.stop()
    assert result == {"multiple": 1.8, "n": 15, "is_default": False}
    bt.assert_not_called()


def test_get_target_multiple_recomputes_when_cache_stale(fresh_db):
    db.set_app_config("orb_target_multiple_date", "2026-07-01")
    db.set_app_config("orb_target_multiple", "1.8")
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    p = _patched_now(_FIXED_NOW)
    try:
        with mock.patch(
            "backend.src.services.analytics.orb_report.backtest_orb_target_multiple",
            new=mock.AsyncMock(return_value={"multiple": 2.2, "n": 12, "is_default": False}),
        ) as bt:
            result = asyncio.run(SimulationEngine._get_orb_target_multiple(e))
    finally:
        p.stop()
    assert result == {"multiple": 2.2, "n": 12, "is_default": False}
    bt.assert_called_once()
    assert db.get_app_config("orb_target_multiple_date") == "2026-07-20"
    assert db.get_app_config("orb_target_multiple") == "2.2"


# ── _backtest_orb_target_multiple ───────────────────────────────────────────

class _ShapedBackfillBridge:
    """For any requested window, returns 2390-2410 range candles for the
    first 8h and a shaped horizon for the rest, regardless of absolute date."""
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
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _ShapedBackfillBridge("clean_bull")
    monday = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)
    p = _patched_now(monday)
    try:
        result = asyncio.run(SimulationEngine._backtest_orb_target_multiple(e))
    finally:
        p.stop()
    assert result == {"multiple": 1.5, "n": 17, "is_default": False}


def test_backtest_no_clean_breakouts_falls_back_to_default(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _ShapedBackfillBridge("ambiguous")
    monday = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)
    p = _patched_now(monday)
    try:
        result = asyncio.run(SimulationEngine._backtest_orb_target_multiple(e))
    finally:
        p.stop()
    assert result == {"multiple": 2.0, "n": 0, "is_default": True}


# ── _orb_auto_execute ────────────────────────────────────────────────────────
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
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    fake_ea = _FakeEA()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=False), \
         mock.patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))
    assert fake_ea.calls == []
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


def test_auto_execute_direction_inside_creates_no_signal(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    fake_ea = _FakeEA()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(SimulationEngine._orb_auto_execute(e, {"direction": "inside"}))
    assert fake_ea.calls == []
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


def test_auto_execute_ea_unhealthy_creates_no_signal(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    fake_ea = _FakeEA(healthy=False)
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))
    assert fake_ea.calls == []
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0


def test_auto_execute_bullish_places_pending_order_at_near_edge(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    fake_ea = _FakeEA()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))

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


def test_auto_execute_uses_orb_lot_size_risk_setting(fresh_db):
    rs = db.get_risk_settings()
    rs["orb_lot_size"] = 0.05
    db.update_risk_settings(rs)
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    fake_ea = _FakeEA()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))
    assert fake_ea.calls[0]["lot_size"] == 0.05
    with db.db() as conn:
        lot = conn.execute("SELECT lot_size FROM vantage_signals").fetchone()[0]
    assert lot == 0.05


def test_auto_execute_place_pending_order_failure_does_not_raise(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()

    class _RaisingEA(_FakeEA):
        async def place_pending_order(self, *a, **kw):
            raise ConnectionError("EA send failed")
    fake_ea = _RaisingEA()
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        asyncio.run(SimulationEngine._orb_auto_execute(e, _BULLISH_REPORT))  # must not raise
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]
    assert n == 0
