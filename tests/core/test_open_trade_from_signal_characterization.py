"""Characterizes the BACK half of open_trade_from_signal on SimulationEngine
(core/engine.py) -- atomic claim, the open_trade call, and the 6 post-fill
strategy override branches -- before task 020 extracts it into
core_open_trade_from_signal.py. See
docs/todo/refactor/core-open-trade-from-signal-migration/010-*.md.

Uses a fake bridge (get_tick/place_order/modify_order/get_account) and,
for the EA-managed branches, ea_bridge.set_instance(fake). NO real or demo
MT5 order is ever placed or modified -- verified via each fake's own call
log.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from forex_trader.core import database as db
from forex_trader.core import ea_bridge
from forex_trader.core.engine import SimulationEngine
from forex_trader.core.models import (
    STRATEGY_SCALE_OUT, STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER,
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_TRAIL_STOP, STRATEGY_REVERSAL_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER,
)


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
    ea_bridge.set_instance(None)


class _FakeBridge:
    def __init__(self, tick="default", order_result=None, account=None):
        self._tick = _default_tick() if tick == "default" else tick
        self._order_result = order_result or {"ticket": 999, "fill_price": 2400.0}
        self._account = account if account is not None else {"balance": 0}
        self.place_order_calls = []
        self.modify_order_calls = []

    async def get_tick(self):
        return self._tick

    async def get_fresh_tick(self):
        return self._tick

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.place_order_calls.append(
            {"direction": direction, "lots": lots, "sl": sl, "tp": tp}
        )
        return self._order_result

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}

    async def get_account(self):
        return self._account


def _default_tick():
    return SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0)


class _FakeEA:
    def __init__(self, ack=None):
        self._ack = ack or {"type": "trade_opened", "ticket": 8001, "fill_price": 2400.0}
        self.update_trade_calls = []

    def is_ea_healthy(self):
        return True

    def is_strategy_portable(self, strategy):
        return True

    async def open_trade(self, trade_id, direction, lot_size, stop_loss, tps, strategy,
                         pcts=None, be_at_pos=None, trail_mode=None):
        return self._ack

    async def update_trade(self, trade_id, tps):
        self.update_trade_calls.append({"trade_id": trade_id, "tps": tps})
        return True


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._dpm_candles = None
    e._cfg = {}
    return e


def _insert_signal(sig_id="sig-1", direction="BUY", entry_low=2399.0, entry_high=2401.0,
                   stop_loss=2390.0, tp1=2410.0, status="pending", source_name="TestChannel",
                   lot_size=0.10):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, tp1, status, created_at, lot_size) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sig_id, source_name, direction, entry_low, entry_high, stop_loss, tp1,
             status, time.time(), lot_size),
        )


def _get_trade_by_signal(signal_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute(
                "SELECT * FROM vantage_simulated_trades WHERE signal_id=?", (signal_id,)
            ).fetchone()
        )


# ── atomic claim ───────────────────────────────────────────────────────────────

def test_atomic_claim_blocks_concurrent_duplicate(fresh_db, engine):
    """Simulates the real race the atomic claim guards against: a signal
    that's still 'pending' when this call's own read-only checks begin
    (so it passes the front-half status validation), but gets claimed by
    another caller before this call reaches its own atomic UPDATE.
    db_module.get_circuit_breaker_state() is an early, single call inside
    the front half -- patched to sneak in the status flip as a side effect
    right before returning its normal value, deterministically reproducing
    the TOCTOU window without needing real concurrency."""
    _insert_signal(status="pending")
    real_get_cb = db.get_circuit_breaker_state

    def _steal_claim(*a, **kw):
        with db.db() as conn:
            conn.execute(
                "UPDATE vantage_signals SET status='activating' WHERE signal_id=?", ("sig-1",)
            )
        return real_get_cb(*a, **kw)

    with patch("forex_trader.core.database.get_circuit_breaker_state", side_effect=_steal_claim):
        with pytest.raises(ValueError, match="duplicate suppressed"):
            asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_restores_pending_on_open_trade_failure(fresh_db, engine):
    _insert_signal()
    engine._bridge = _FakeBridge(order_result={"error": "requote"})
    with pytest.raises(RuntimeError):
        asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))
    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-1",)
        ).fetchone()[0]
    assert status == "pending"


# ── post-fill overrides ──────────────────────────────────────────────────────────

def test_conservative_post_fill_override(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE})
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})
    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    trade = _get_trade_by_signal("sig-1")
    assert trade["stop_loss"] == 2395.5   # fill - 5
    assert trade["tp1"] == 2403.5         # fill + 3
    assert trade["tp2"] is None
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.5, "tp": None}]


def test_conservative_ea_managed_calls_update_trade(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE, "ea_bridge_enabled": 1})
    fake_ea = _FakeEA(ack={"type": "trade_opened", "ticket": 8001, "fill_price": 2400.5})
    ea_bridge.set_instance(fake_ea)
    engine._bridge = _FakeBridge()

    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    assert fake_ea.update_trade_calls == [
        {"trade_id": _get_trade_by_signal("sig-1")["trade_id"], "tps": {1: 2403.5}}
    ]


def test_conservative_python_managed_never_calls_ea_update_trade(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE})
    ea_bridge.set_instance(None)  # nothing configured -- python bridge path
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})

    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))
    # no exception, no EA touched -- ea_bridge.get_instance() naturally None


def test_scalp_runner_post_fill_override(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALP_RUNNER})
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})
    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    trade = _get_trade_by_signal("sig-1")
    assert trade["stop_loss"] == 2390.5   # fill - 10
    assert trade["tp1"] == 2403.5         # fill + 3
    assert trade["tp2"] == 2404.5         # fill + 4
    assert trade["tp3"] is None


def test_scalp_runner_ea_managed_calls_update_trade_with_both_tps(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALP_RUNNER, "ea_bridge_enabled": 1})
    fake_ea = _FakeEA(ack={"type": "trade_opened", "ticket": 8001, "fill_price": 2400.5})
    ea_bridge.set_instance(fake_ea)
    engine._bridge = _FakeBridge()

    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    assert fake_ea.update_trade_calls == [
        {"trade_id": _get_trade_by_signal("sig-1")["trade_id"], "tps": {1: 2403.5, 2: 2404.5}}
    ]


def test_conservative_trial_post_fill_override(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, lot_size=0.10)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE_TRIAL})
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})
    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    trade = _get_trade_by_signal("sig-1")
    assert trade["stop_loss"] == 2390.5   # fill - 10.0 (100/(0.10*100))
    assert trade["tp1"] == 2405.5         # fill + 5
    assert trade["tp6"] == 2435.5         # fill + 35
    assert trade["tp7"] is None


def test_reversal_runner_post_fill_override_leaves_tps_untouched(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=None)
    db.update_risk_settings({"trade_strategy": STRATEGY_REVERSAL_RUNNER})
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})
    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    trade = _get_trade_by_signal("sig-1")
    # stated dist 5pt -> widened to min(5*4,20)=20pt from fill
    assert trade["stop_loss"] == 2380.5
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2380.5, "tp": None}]


def test_adaptive_runner_post_fill_override(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=2410.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_ADAPTIVE_RUNNER})
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})
    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    trade = _get_trade_by_signal("sig-1")
    # stated dist 5pt -> widened 20pt, capped at 50% of final TP dist from fill (2410-2400.5=9.5 -> cap 4.75)
    # max(5, min(20, 4.75)) = 5
    assert trade["stop_loss"] == 2395.5  # fill - 5


def test_trail_stop_post_fill_override(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({
        "trade_strategy": STRATEGY_TRAIL_STOP, "trail_stop_sl_pts": 6.0,
        "trailing_stop_distance": 3.0,
    })
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})
    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    trade = _get_trade_by_signal("sig-1")
    assert trade["stop_loss"] == 2394.5   # fill - 6
    assert trade["tp1"] == 2403.5         # fill + 1*3
    assert trade["tp8"] == 2424.5         # fill + 8*3


def test_scale_out_has_no_post_fill_override(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALE_OUT})
    engine._bridge = _FakeBridge(order_result={"ticket": 555, "fill_price": 2400.5})
    asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))

    trade = _get_trade_by_signal("sig-1")
    assert trade["stop_loss"] == 2390.0  # untouched, from open_trade's placement
    assert engine._bridge.modify_order_calls == []


# ── background commentary scheduling ──────────────────────────────────────────

def test_background_commentary_scheduled_for_local_trade(fresh_db, engine):
    _insert_signal()
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALE_OUT})
    engine._bridge = _FakeBridge()
    # Just confirm the call completes without raising -- _background_open_commentary
    # is itself out of scope (deferred), fire-and-forget via asyncio.create_task.
    result = asyncio.run(SimulationEngine.open_trade_from_signal(engine, "sig-1"))
    assert result.get("trade_id")
