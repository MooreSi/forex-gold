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

from backend.src.db import database as db
from backend.src.services.dpm import engine as dpm_engine
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
from backend.src.services.dpm.bookkeeping import DPMCache as _DPMCache
from backend.src.runtime import TradingRuntime


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
    e = TradingRuntime.__new__(TradingRuntime)
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
        asyncio.run(TradingRuntime._handle_dynamic_position_management(engine, trade, tick))


# ── _handle_dynamic_position_management ──────────────────────────────────────

# ── _run_dpm_calibration ──────────────────────────────────────────────────────

def _closed_dpm_trades(n):
    return [{"closed_at": time.time(), "final_pnl": 10.0} for _ in range(n)]


