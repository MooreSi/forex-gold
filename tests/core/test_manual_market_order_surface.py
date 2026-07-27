"""Proves forex_trader.core.core_manual_market_order's extracted function
behaves identically to SimulationEngine.open_manual_market_order,
characterized in test_manual_market_order_characterization.py -- see
docs/todo/refactor/core-manual-market-order-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed -- verified via the fake
bridge's own call log.
"""
import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_manual_market_order as mmo
from backend.src.utils.models import STRATEGY_TRAIL_STOP


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
    def __init__(self, tick="default", order_result=None, account=None, candles=None):
        self._tick = _default_tick() if tick == "default" else tick
        self._order_result = order_result or {"ticket": 999, "fill_price": 2400.0}
        self._account = account if account is not None else {"balance": 0}
        self._candles = candles if candles is not None else []
        self.place_order_calls = []

    async def get_tick(self):
        return self._tick

    async def get_fresh_tick(self):
        return self._tick

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.place_order_calls.append(
            {"direction": direction, "lots": lots, "sl": sl, "tp": tp}
        )
        return self._order_result

    async def get_account(self):
        return self._account

    async def get_candles(self, timeframe="M5", count=200):
        return self._candles


def _default_tick():
    return SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0)


def _get_signal_row(signal_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone()
        )


def _sig_id_for_trade(trade_id):
    with db.db() as conn:
        return conn.execute(
            "SELECT signal_id FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()[0]


# ── validation ─────────────────────────────────────────────────────────────────

def test_raises_on_invalid_direction(fresh_db):
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Invalid direction"):
        asyncio.run(mmo.open_manual_market_order(bridge, "SIDEWAYS", stop_loss=2390.0))


def test_raises_when_no_live_tick(fresh_db):
    bridge = _FakeBridge(tick=None)
    with pytest.raises(RuntimeError, match="No live price"):
        asyncio.run(mmo.open_manual_market_order(bridge, "BUY", stop_loss=2390.0))


# ── SL resolution ──────────────────────────────────────────────────────────────

def test_explicit_sl_used_when_plausible(fresh_db):
    bridge = _FakeBridge()
    result = asyncio.run(mmo.open_manual_market_order(bridge, "BUY", stop_loss=2390.0))
    sig = _get_signal_row(_sig_id_for_trade(result["trade_id"]))
    assert sig["stop_loss"] == 2390.0


def test_explicit_sl_too_close_raises(fresh_db):
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="implausible"):
        asyncio.run(mmo.open_manual_market_order(bridge, "BUY", stop_loss=2399.9))


def test_explicit_sl_too_far_raises(fresh_db):
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="implausible"):
        asyncio.run(mmo.open_manual_market_order(bridge, "BUY", stop_loss=1000.0))


def test_no_sl_dpm_enabled_uses_atr_based_sl(fresh_db):
    db.update_risk_settings({"dpm_enabled": 1})
    bridge = _FakeBridge(candles=[{"h": 1, "l": 1, "c": 1}])
    with patch("forex_trader.core.dpm_engine.compute_atr", return_value=10.0):
        result = asyncio.run(mmo.open_manual_market_order(bridge, "BUY"))
    sig = _get_signal_row(_sig_id_for_trade(result["trade_id"]))
    assert sig["stop_loss"] == 2388.2


def test_no_sl_dpm_enabled_falls_back_to_fixed_distance_without_atr(fresh_db):
    db.update_risk_settings({"dpm_enabled": 1})
    bridge = _FakeBridge(candles=[])
    result = asyncio.run(mmo.open_manual_market_order(bridge, "BUY"))
    sig = _get_signal_row(_sig_id_for_trade(result["trade_id"]))
    assert sig["stop_loss"] == 2392.2


def test_no_sl_dpm_disabled_raises(fresh_db):
    db.update_risk_settings({"dpm_enabled": 0})
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Stop Loss is required"):
        asyncio.run(mmo.open_manual_market_order(bridge, "BUY"))


# ── lot sizing ─────────────────────────────────────────────────────────────────

def test_explicit_lot_size_used(fresh_db):
    bridge = _FakeBridge()
    asyncio.run(mmo.open_manual_market_order(bridge, "BUY", stop_loss=2390.0, lot_size=0.25))
    assert bridge.place_order_calls[0]["lots"] == 0.25


def test_strategy_fixed_lot_used_when_no_explicit_lot(fresh_db):
    db.update_risk_settings({"strategy_lot_size": 0.33})
    bridge = _FakeBridge()
    asyncio.run(mmo.open_manual_market_order(bridge, "BUY", stop_loss=2390.0))
    assert bridge.place_order_calls[0]["lots"] == 0.33


def test_risk_based_lot_when_neither_set(fresh_db):
    bridge = _FakeBridge()
    asyncio.run(mmo.open_manual_market_order(bridge, "BUY", stop_loss=2390.0))
    assert bridge.place_order_calls[0]["lots"] > 0


# ── signal record + open_trade wiring ─────────────────────────────────────────

def test_creates_backing_signal_and_calls_open_trade(fresh_db):
    bridge = _FakeBridge(order_result={"ticket": 777, "fill_price": 2400.2})
    result = asyncio.run(mmo.open_manual_market_order(
        bridge, "BUY", stop_loss=2390.0, strategy=STRATEGY_TRAIL_STOP,
        take_profit=2420.0, source_name="orb_report"))

    sig = _get_signal_row(_sig_id_for_trade(result["trade_id"]))
    assert sig["source_name"] == "Manual Market Order"
    assert sig["status"] == "active"
    assert sig["stop_loss"] == 2390.0
    assert sig["tp1"] == 2420.0

    assert result["mt5_ticket"] == 777
    assert result["strategy"] == STRATEGY_TRAIL_STOP
    assert bridge.place_order_calls[0]["direction"] == "BUY"
    assert bridge.place_order_calls[0]["tp"] is None
