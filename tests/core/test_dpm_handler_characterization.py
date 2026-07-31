"""Characterizes _handle_dynamic_position_management/_run_dpm_calibration
on SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-dpm-handler-migration/010-*.md.

Uses a fake bridge (partial_close/modify_order) and patches
dpm_engine.compute_adaptive_params/run_calibration -- both are an
already-extracted, stable, pure module, treated as an external
collaborator here, same as `bridge`. NO real or demo MT5 order is ever
placed, closed, or modified -- verified via the fake's own call log.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from backend.src.services.dpm import engine as dpm_engine
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
from backend.src.services.dpm.bookkeeping import DPMCache as _DPMCache
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
    def __init__(self, partial_close_result=None):
        self._result = partial_close_result or {"success": True, "close_price": None, "lots_closed": None}
        self.partial_close_calls = []
        self.modify_order_calls = []

    async def partial_close(self, ticket, lots):
        self.partial_close_calls.append({"ticket": ticket, "lots": lots})
        result = dict(self._result)
        if result.get("lots_closed") is None:
            result["lots_closed"] = lots
        if result.get("close_price") is None:
            result.pop("close_price", None)
        return result

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._tp_trigger_cache = _TPCache()
    e._dpm_cache = _DPMCache()
    e._dpm_candles = []
    e._dpm_dxy_candles = None
    return e


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


_BASE_PARAMS = {
    "atr": 8.0, "session": "London", "momentum": 0.6, "momentum_label": "strong",
    "regime": "trending", "adx": 30.0, "be_multiplier": 1.5, "trail_multiplier": 1.2,
    "be_trigger_usd": 20.0, "trail_distance": 5.0, "tp1_partial_pct": 0.5,
    "used_calibrated": True, "swing_sl": None, "reasoning": "test",
}


def _params(**overrides):
    p = dict(_BASE_PARAMS)
    p.update(overrides)
    return p


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2380.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", mt5_ticket=None,
                  lot_size=0.10, remaining_lots=0.10, stop_loss=2400.0,
                  entry_price=2400.0, sl_moved_to_be=0, **tps):
    with db.db() as conn:
        cols = ("trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
                "entry_price, lot_size, remaining_lots, stop_loss, sl_moved_to_be, "
                "status, open_time")
        vals = [trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, entry_price,
                lot_size, remaining_lots, stop_loss, sl_moved_to_be, "open", time.time()]
        for k, v in tps.items():
            cols += f", {k}"
            vals.append(v)
        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({placeholders})", vals)


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def _dpm_row(trade_id):
    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM dpm_trade_performance WHERE trade_id=?", (trade_id,)
        ).fetchone()
        return db.row_to_dict(row) if row else None


def _partial_close_reasons(trade_id):
    with db.db() as conn:
        rows = conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE trade_id=? ORDER BY ts", (trade_id,)
        ).fetchall()
        return [r[0] for r in rows]


def _run_handler(engine, trade, tick, params):
    with mock.patch.object(dpm_engine, "compute_adaptive_params", return_value=params):
        asyncio.run(SimulationEngine._handle_dynamic_position_management(engine, trade, tick))


# ── _handle_dynamic_position_management ──────────────────────────────────────

def test_tp1_cleared_partial_closes_and_moves_sl_to_be(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0, sl_moved_to_be=0, tp1=2405.0)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2406.0, ask=2406.5), _params(tp1_partial_pct=0.5))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.05}]
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1
    assert _dpm_row("t-1")["reached_tp1"] == 1


def test_tp1_cleared_zero_pct_no_partial_close_but_sl_still_moves(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0, sl_moved_to_be=0, tp1=2405.0)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2406.0, ask=2406.5), _params(tp1_partial_pct=0.0))

    assert engine._bridge.partial_close_calls == []
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert _dpm_row("t-1")["reached_tp1"] == 1


def test_tp1_cleared_already_at_be_skips_sl_move_block(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1, tp1=2405.0)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2406.0, ask=2406.5), _params(tp1_partial_pct=0.5))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.05}]
    assert engine._bridge.modify_order_calls == []
    assert _trade_dict("t-1")["stop_loss"] == 2400.0


def test_tp1_bridge_rejection_returns_immediately_no_milestone(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0, sl_moved_to_be=0, tp1=2405.0)
    trade = _trade_dict("t-1")
    engine._bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    _run_handler(engine, trade, _tick(bid=2406.0, ask=2406.5), _params(tp1_partial_pct=0.5))

    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2380.0
    assert trade_after["sl_moved_to_be"] == 0
    dpm_row = _dpm_row("t-1")
    assert dpm_row is not None  # _record_dpm_entry always runs, before the TP1 branch
    assert dpm_row["reached_tp1"] == 0


def test_be_trigger_crossed_moves_sl_sets_milestone(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0, sl_moved_to_be=0, tp1=2450.0)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2401.0, ask=2401.5), _params(be_trigger_usd=5.0))

    assert engine._bridge.partial_close_calls == []
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1
    assert _dpm_row("t-1")["reached_be"] == 1


def test_sl_moved_to_be_db_read_fallback_skips_to_trailing(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    trade["sl_moved_to_be"] = 0  # simulate a one-cycle-stale caller dict
    _run_handler(engine, trade, _tick(bid=2420.0, ask=2420.5), _params(trail_distance=5.0))

    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2415.0, "tp": None}]
    assert _trade_dict("t-1")["stop_loss"] == 2415.0


def test_trailing_arithmetic_when_price_move_clears_full_distance(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2420.0, ask=2420.5), _params(trail_distance=5.0))

    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2415.0, "tp": None}]
    assert _trade_dict("t-1")["stop_loss"] == 2415.0


def test_trailing_tightened_when_full_distance_not_yet_cleared(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2410.0, ask=2410.5), _params(trail_distance=15.0))

    # price_move=10 < trail_distance=15 -> effective_trail = round(10*0.60, 2) = 6.0
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2404.0, "tp": None}]
    assert _trade_dict("t-1")["stop_loss"] == 2404.0


def test_trailing_swing_sl_wins_when_further_than_arithmetic(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2420.0, ask=2420.5),
                 _params(trail_distance=5.0, swing_sl=2418.0))

    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2418.0, "tp": None}]
    assert _trade_dict("t-1")["stop_loss"] == 2418.0


def test_trailing_should_update_false_no_modify_order(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2416.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2420.0, ask=2420.5), _params(trail_distance=5.0))

    assert engine._bridge.modify_order_calls == []
    assert _trade_dict("t-1")["stop_loss"] == 2416.0


def test_tp2_marker_inserted_once_trailing_locked(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1, tp2=2410.0)
    trade = _trade_dict("t-1")
    _run_handler(engine, trade, _tick(bid=2411.0, ask=2411.5), _params(trail_distance=5.0))

    assert engine._bridge.partial_close_calls == []
    assert _partial_close_reasons("t-1") == ["TP2_DPM_MARKER"]


# ── _run_dpm_calibration ──────────────────────────────────────────────────────

def _closed_dpm_trades(n):
    return [{"closed_at": time.time(), "final_pnl": 10.0} for _ in range(n)]


def test_calibration_skips_within_2hr_gap(fresh_db, engine):
    db.set_app_config("dpm_cal_last_run", str(time.time() - 100))
    db.set_app_config("dpm_cal_trade_count", "0")
    with mock.patch.object(dpm_engine, "run_calibration") as m:
        asyncio.run(SimulationEngine._run_dpm_calibration(engine))
    m.assert_not_called()


def test_calibration_skips_below_20_closed_trades(fresh_db, engine):
    db.set_app_config("dpm_cal_last_run", str(time.time() - 8000))
    db.set_app_config("dpm_cal_trade_count", "0")
    with db.db() as conn:
        for i in range(10):
            conn.execute(
                "INSERT INTO dpm_trade_performance (trade_id, closed_at, final_pnl) VALUES (?,?,?)",
                (f"t-{i}", time.time(), 5.0),
            )
    with mock.patch.object(dpm_engine, "run_calibration") as m:
        asyncio.run(SimulationEngine._run_dpm_calibration(engine))
    m.assert_not_called()


def test_calibration_skips_below_5_new_trades_since_last_count(fresh_db, engine):
    db.set_app_config("dpm_cal_last_run", str(time.time() - 8000))
    db.set_app_config("dpm_cal_trade_count", "18")
    with db.db() as conn:
        for i in range(20):
            conn.execute(
                "INSERT INTO dpm_trade_performance (trade_id, closed_at, final_pnl) VALUES (?,?,?)",
                (f"t-{i}", time.time(), 5.0),
            )
    with mock.patch.object(dpm_engine, "run_calibration") as m:
        asyncio.run(SimulationEngine._run_dpm_calibration(engine))
    m.assert_not_called()


def test_calibration_runs_and_persists_results(fresh_db, engine):
    db.set_app_config("dpm_cal_last_run", str(time.time() - 8000))
    db.set_app_config("dpm_cal_trade_count", "0")
    with db.db() as conn:
        for i in range(20):
            conn.execute(
                "INSERT INTO dpm_trade_performance (trade_id, closed_at, final_pnl) VALUES (?,?,?)",
                (f"t-{i}", time.time(), 5.0),
            )
    result = {
        "calibrated_at": time.time(), "session": "London", "momentum_bucket": "strong",
        "be_multiplier": 1.4, "trail_multiplier": 1.1, "tp1_partial_pct": 0.4,
        "sample_size": 20, "profit_factor": 1.8, "win_rate": 0.6, "avg_r_multiple": 1.2,
        "notes": "test",
    }
    engine._dpm_cache.loaded_at = 12345.0
    with mock.patch.object(dpm_engine, "run_calibration", return_value=[result]):
        asyncio.run(SimulationEngine._run_dpm_calibration(engine))

    with db.db() as conn:
        rows = conn.execute("SELECT * FROM dpm_calibration").fetchall()
    assert len(rows) == 1
    assert db.get_app_config("dpm_cal_trade_count") == "20"
    assert float(db.get_app_config("dpm_cal_last_run")) > time.time() - 10
    assert engine._dpm_cache.loaded_at == 0.0  # forced reload


def test_calibration_empty_results_does_not_update_app_config(fresh_db, engine):
    db.set_app_config("dpm_cal_last_run", str(time.time() - 8000))
    db.set_app_config("dpm_cal_trade_count", "0")
    with db.db() as conn:
        for i in range(20):
            conn.execute(
                "INSERT INTO dpm_trade_performance (trade_id, closed_at, final_pnl) VALUES (?,?,?)",
                (f"t-{i}", time.time(), 5.0),
            )
    with mock.patch.object(dpm_engine, "run_calibration", return_value=[]):
        asyncio.run(SimulationEngine._run_dpm_calibration(engine))

    with db.db() as conn:
        rows = conn.execute("SELECT * FROM dpm_calibration").fetchall()
    assert rows == []
    assert db.get_app_config("dpm_cal_trade_count") == "0"
