"""Characterizes _process_instant_entry on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-instant-entry-migration/010-*.md.

open_trade (already extracted, pack 11) is mocked in every test here --
its own real behavior was already characterized in its own extraction
pack. NO real or demo MT5 order is ever placed, closed, or modified --
open_trade is never given a real bridge to act through.
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import core_instant_entry
from forex_trader.core import database as db
from forex_trader.core.engine import SimulationEngine


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


class _FakeBridge:
    def __init__(self):
        self.modify_order_calls = []
        self.get_tick = mock.AsyncMock(return_value=None)

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._dpm_candles = []
    e._cfg = {}
    return e


_FRESH_TS = datetime.now(timezone.utc).isoformat()
_STALE_TS = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
_TICK = SimpleNamespace(bid=2414.5, ask=2415.0, spread_points=10.0)
_TRADE_RESULT = {"entry_price": 2415.0, "mt5_ticket": 777, "trade_id": "trade-abc", "managed_by": "app"}


def _rs():
    return db.get_risk_settings()


def _signals_count():
    with db.db() as conn:
        return conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]


def _tg_status(tg_id):
    with db.db() as conn:
        row = conn.execute(
            "SELECT status FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)
        ).fetchone()
        return row[0] if row else None


def _run(engine, msg, tg_id, direction, price, rs, auto_execute, text="XAU Buy Now"):
    return asyncio.run(SimulationEngine._process_instant_entry(
        engine, msg, tg_id, "grp-1", "Chan", text, direction, price, rs, auto_execute,
    ))


def test_stale_message_recorded_historical_no_signal(fresh_db, engine):
    _run(engine, {"timestamp": _STALE_TS}, "tg-1", "BUY", None, _rs(), True)
    assert _tg_status("tg-1") == "instant_historical"
    assert _signals_count() == 0


def test_no_timestamp_treated_as_stale(fresh_db, engine):
    _run(engine, {}, "tg-2", "BUY", None, _rs(), True)
    assert _tg_status("tg-2") == "instant_historical"


def test_auto_execute_off_recorded_pending_no_signal(fresh_db, engine):
    _run(engine, {"timestamp": _FRESH_TS}, "tg-3", "BUY", None, _rs(), False)
    assert _tg_status("tg-3") == "instant_pending"
    assert _signals_count() == 0


def test_session_blocked_no_signal(fresh_db, engine):
    with mock.patch.object(db, "is_session_allowed", return_value=(False, "Asian")):
        _run(engine, {"timestamp": _FRESH_TS}, "tg-4", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def test_no_tick_no_signal(fresh_db, engine):
    _run(engine, {"timestamp": _FRESH_TS}, "tg-5", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def test_wide_spread_no_signal(fresh_db, engine):
    wide_tick = SimpleNamespace(bid=2414.5, ask=2415.0, spread_points=100.0)
    engine._bridge.get_tick = mock.AsyncMock(return_value=wide_tick)
    _run(engine, {"timestamp": _FRESH_TS}, "tg-6", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def test_max_open_trades_reached_no_signal(fresh_db, engine):
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_open_trades", return_value=[{"trade_id": "x"}]):
        _run(engine, {"timestamp": _FRESH_TS}, "tg-7", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def _run_open_trade_path(engine, rs, tg_id="tg-8"):
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)) as ot:
        _run(engine, {"timestamp": _FRESH_TS}, tg_id, "BUY", None, rs, True)
    return ot


def test_default_risk_pct_sizing_floors_to_min_lot(fresh_db, engine):
    ot = _run_open_trade_path(engine, _rs())
    assert ot.called
    assert ot.call_args.kwargs["lot_size"] == 0.01
    assert ot.call_args.kwargs["stop_loss"] == 2403.0  # 12pt default ATR-unavailable distance


def test_governor_on_default_settings_skips_entirely(fresh_db, engine):
    rs = dict(_rs(), risk_governor_enabled=1, strategy_lot_size=0.0)
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)) as ot:
        _run(engine, {"timestamp": _FRESH_TS}, "tg-9", "BUY", None, rs, True)
    assert not ot.called


def test_governor_on_max_risk_cap_binds_over_raw_risk_pct(fresh_db, engine):
    rs = dict(_rs(), risk_governor_enabled=1, strategy_lot_size=0.0,
              risk_per_trade_pct=5.0, max_risk_per_trade_pct=1.0)
    ot = _run_open_trade_path(engine, rs, tg_id="tg-10")
    assert ot.called
    assert ot.call_args.kwargs["lot_size"] == 0.01
    assert ot.call_args.kwargs["stop_loss"] == 2403.0


def test_governor_on_fixed_lot_uses_it_directly(fresh_db, engine):
    rs = dict(_rs(), risk_governor_enabled=1, strategy_lot_size=0.05)
    ot = _run_open_trade_path(engine, rs, tg_id="tg-11")
    assert ot.call_args.kwargs["lot_size"] == 0.05
    assert ot.call_args.kwargs["stop_loss"] == 2403.0


def test_governor_off_fixed_lot_uses_150_cap_distance(fresh_db, engine):
    rs = dict(_rs(), risk_governor_enabled=0, strategy_lot_size=0.05)
    ot = _run_open_trade_path(engine, rs, tg_id="tg-12")
    assert ot.call_args.kwargs["lot_size"] == 0.05
    assert ot.call_args.kwargs["stop_loss"] == 2390.0  # 25pt $150-cap-clamped distance


def test_channel_override_auto_uses_last_claude_rec(fresh_db, engine):
    db.set_channel_strategy_override("Chan", "auto")
    db.set_channel_strategy_rec("Chan", "gd_vip_runner", "trending", 0.8)
    ot = _run_open_trade_path(engine, _rs(), tg_id="tg-13")
    assert ot.call_args.kwargs["strategy"] == "gd_vip_runner"


def test_channel_override_explicit_used_directly(fresh_db, engine):
    db.set_channel_strategy_override("Chan", "trail_stop")
    ot = _run_open_trade_path(engine, _rs(), tg_id="tg-14")
    assert ot.call_args.kwargs["strategy"] == "trail_stop"


def test_high_risk_text_forces_conservative(fresh_db, engine):
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)) as ot:
        _run(engine, {"timestamp": _FRESH_TS}, "tg-15", "BUY", None, _rs(), True,
             text="XAU Buy Now — High Risk")
    assert ot.call_args.kwargs["strategy"] == "conservative"


def _insert_open_trade_for_postfill(trade_id="trade-abc"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-x", "BUY", 2415.0, 2415.0, 2403.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "sig-x", 777, "BUY", 2415.0, 2415.0, 2415.0, 0.01, 0.01, 2403.0,
             "open", time.time()),
        )


def test_conservative_post_fill_overrides_sl_tp1_from_fill(fresh_db, engine):
    _insert_open_trade_for_postfill()
    rs = dict(_rs(), trade_strategy="conservative")
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)):
        _run(engine, {"timestamp": _FRESH_TS}, "tg-16", "BUY", None, rs, True)

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", ("trade-abc",)).fetchone()
        )
    assert row["stop_loss"] == 2410.0  # 5pt below fill (2415)
    assert row["tp1"] == 2418.0        # 3pt above fill
    assert row["tp2"] is None
    assert engine._bridge.modify_order_calls == [{"ticket": 777, "sl": 2410.0, "tp": None}]


def test_conservative_trial_post_fill_sets_six_tp_ladder(fresh_db, engine):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-x", "BUY", 2415.0, 2415.0, 2403.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trade-abc", "sig-x", 777, "BUY", 2415.0, 2415.0, 2415.0, 0.10, 0.10, 2403.0,
             "open", time.time()),
        )
    rs = dict(_rs(), trade_strategy="conservative_trial", strategy_lot_size=0.10)
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)):
        _run(engine, {"timestamp": _FRESH_TS}, "tg-17", "BUY", None, rs, True)

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", ("trade-abc",)).fetchone()
        )
    assert row["stop_loss"] == 2405.0
    assert [row[f"tp{i}"] for i in range(1, 7)] == [2420.0, 2425.0, 2429.0, 2435.0, 2442.0, 2450.0]


def test_open_trade_exception_rolls_back_signal_and_marks_failed(fresh_db, engine):
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade",
                           new=mock.AsyncMock(side_effect=RuntimeError("mt5 fail"))):
        _run(engine, {"timestamp": _FRESH_TS}, "tg-18", "BUY", None, _rs(), True)

    assert _signals_count() == 0
    assert _tg_status("tg-18") == "instant_failed"


def test_circuit_breaker_exception_same_cleanup_as_generic(fresh_db, engine):
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade",
                           new=mock.AsyncMock(side_effect=ValueError("circuit breaker tripped"))):
        _run(engine, {"timestamp": _FRESH_TS}, "tg-19", "BUY", None, _rs(), True)

    assert _signals_count() == 0
    assert _tg_status("tg-19") == "instant_failed"
