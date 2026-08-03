"""Characterizes close_trade/_record_close/_close_all_ladder_legs/
_get_trading_balance on SimulationEngine (core/engine.py) before task 020
extracts them -- see docs/todo/refactor/core-close-trade-migration/010-*.md.

Uses a fake bridge test-double (get_tick/close_position/get_account) --
NO real or demo MT5 order is ever placed, closed, or modified. All bridge
interaction is verified via the fake's own call log.

push_trade_closed (ledger) and telegram_alerts.send_message are called for
real, not mocked -- both are confirmed safe against a test DB: the ledger
push only writes local tables and no-ops with no sync server running; the
Telegram send short-circuits before any HTTP call since telegram_config.
enabled defaults to 0 in a fresh schema.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
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


class _FakeBridge:
    def __init__(self, tick=None, close_results=None, account=None):
        self._tick = tick
        self._close_results = close_results or {}
        self._default_close_result = {"success": True, "close_price": 2415.0}
        self._account = account if account is not None else {"balance": 0}
        self.close_position_calls = []

    async def get_tick(self):
        return self._tick

    async def close_position(self, ticket):
        self.close_position_calls.append(ticket)
        return self._close_results.get(ticket, self._default_close_result)

    async def get_account(self):
        return self._account


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._cfg = {"starting_balance": 1000.0}
    e._tp_trigger_cache = _TPCache()
    e._scale_out_last_fail = {}
    e._tp_safety_net_last_alert = {}
    e._profit_sound_seq = 0
    return e


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", status="open",
                  remaining_lots=0.10, mt5_ticket=None, net_pnl=0.0, realised_pnl=0.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, net_pnl, realised_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, 2400.0, 0.10,
             remaining_lots, 2390.0, status, time.time(), net_pnl, realised_pnl),
        )


def _insert_ladder_leg(trade_id, tier, tp_num, tp_price, lots, mt5_ticket, status="open",
                       entry_price=2400.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_ladder_legs (trade_id, tier, tp_num, tp_price, lots, "
            "entry_price, mt5_ticket, status) VALUES (?,?,?,?,?,?,?,?)",
            (trade_id, tier, tp_num, tp_price, lots, entry_price, mt5_ticket, status),
        )


def _get_trade(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


# ── _get_trading_balance ──────────────────────────────────────────────────────

def test_get_trading_balance_prefers_live_mt5_balance(fresh_db, engine):
    engine._bridge = _FakeBridge(account={"balance": 1234.5})
    balance = asyncio.run(SimulationEngine._get_trading_balance(engine))
    assert balance == 1234.5


def test_get_trading_balance_falls_back_to_sim_account(fresh_db, engine):
    engine._bridge = _FakeBridge(account={"balance": 0})
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulation_account SET balance=? WHERE id=1", (777.0,))
    balance = asyncio.run(SimulationEngine._get_trading_balance(engine))
    assert balance == 777.0


# ── close_trade ────────────────────────────────────────────────────────────────

def test_close_trade_raises_when_not_open(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", status="closed")
    with pytest.raises(ValueError):
        asyncio.run(SimulationEngine.close_trade(engine, "t-1"))


def test_close_trade_uses_tick_bid_for_buy_close_price(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=_tick(bid=2415.0, ask=2415.5))
    result = asyncio.run(SimulationEngine.close_trade(engine, "t-1"))
    assert result["close_price"] == 2415.0


def test_close_trade_falls_back_to_entry_price_with_no_tick_no_ticket(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=None)
    result = asyncio.run(SimulationEngine.close_trade(engine, "t-1"))
    assert result["close_price"] == 2400.0  # entry_price


def test_close_trade_calls_bridge_close_position_for_mt5_ticket(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555)
    bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={555: {"success": True, "close_price": 2416.0}},
    )
    engine._bridge = bridge
    result = asyncio.run(SimulationEngine.close_trade(engine, "t-1"))
    assert bridge.close_position_calls == [555]
    assert result["close_price"] == 2416.0


def test_close_trade_raises_on_bridge_error(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555)
    engine._bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={555: {"error": "no such ticket"}},
    )
    with pytest.raises(RuntimeError):
        asyncio.run(SimulationEngine.close_trade(engine, "t-1"))


def test_close_trade_raises_on_bridge_success_false(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555)
    engine._bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={555: {"success": False}},
    )
    with pytest.raises(RuntimeError):
        asyncio.run(SimulationEngine.close_trade(engine, "t-1"))


def test_close_trade_routes_to_ladder_legs_when_any_leg_open(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=100)
    _insert_ladder_leg("t-1", tier=1, tp_num=1, tp_price=2410.0, lots=0.05, mt5_ticket=101, status="open")
    _insert_ladder_leg("t-1", tier=2, tp_num=2, tp_price=2420.0, lots=0.05, mt5_ticket=102, status="closed")
    bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={101: {"success": True, "close_price": 2412.0}},
    )
    engine._bridge = bridge
    result = asyncio.run(SimulationEngine.close_trade(engine, "t-1"))
    # Only the still-open leg (101) gets closed via the bridge -- not the
    # anchor ticket 100 (the normal single-ticket path is skipped entirely).
    assert bridge.close_position_calls == [101]
    trade = _get_trade("t-1")
    assert trade["status"] == "closed"


# ── _record_close (mostly exercised through close_trade, some direct) ─────────

def test_record_close_updates_balance_and_cascades_signal(fresh_db, engine):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=None)
    result = asyncio.run(SimulationEngine.record_close(engine, "t-1", 2410.0, "manual_close"))

    with db.db() as conn:
        balance = conn.execute("SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0]
    assert balance == round(1000.0 + result["net_pnl"], 4)

    with db.db() as conn:
        sig_status = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-1",)).fetchone()[0]
    assert sig_status == "closed"


def test_record_close_invalidates_caches(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=None)
    engine._tp_trigger_cache.triggered["t-1"] = ({1}, time.time())
    engine._scale_out_last_fail[("t-1", 1)] = time.time()
    engine._tp_safety_net_last_alert["t-1"] = time.time()

    asyncio.run(SimulationEngine.record_close(engine, "t-1", 2410.0, "manual_close"))

    assert "t-1" not in engine._tp_trigger_cache.triggered
    assert ("t-1", 1) not in engine._scale_out_last_fail
    assert "t-1" not in engine._tp_safety_net_last_alert


def test_record_close_updates_peak_balance_when_higher(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=None, account={"balance": 5000.0})
    db.set_app_config("peak_balance", "1000.0")

    asyncio.run(SimulationEngine.record_close(engine, "t-1", 2410.0, "manual_close"))

    assert float(db.get_app_config("peak_balance")) == 5000.0


def test_record_close_does_not_lower_peak_balance(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=None, account={"balance": 100.0})
    db.set_app_config("peak_balance", "9000.0")

    asyncio.run(SimulationEngine.record_close(engine, "t-1", 2410.0, "manual_close"))

    assert float(db.get_app_config("peak_balance")) == 9000.0


def test_record_close_skips_risk_governor_when_disabled(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=None)
    db.update_risk_settings({"risk_governor_enabled": 0})
    db.set_app_config("trade_pause_until", "")  # sentinel: should stay untouched/empty

    asyncio.run(SimulationEngine.record_close(engine, "t-1", 2410.0, "manual_close"))

    assert db.get_app_config("trade_pause_until") in (None, "")


def test_record_close_runs_risk_governor_when_enabled(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    engine._bridge = _FakeBridge(tick=None, account={"balance": 100.0})
    db.update_risk_settings({
        "risk_governor_enabled": 1,
        "max_total_drawdown_pct": 5.0,
    })
    db.set_app_config("peak_balance", "10000.0")  # huge drawdown from this peak

    asyncio.run(SimulationEngine.record_close(engine, "t-1", 2410.0, "manual_close"))

    assert db.get_app_config("trade_pause_until") is not None


def test_record_close_skips_circuit_breaker_for_non_mt5_trade(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)  # pure sim trade
    engine._bridge = _FakeBridge(tick=None)
    db.update_risk_settings({"circuit_breaker_enabled": 1, "circuit_breaker_losses": 1})

    asyncio.run(SimulationEngine.record_close(engine, "t-1", 2380.0, "SL"))  # a loss

    rs = db.get_risk_settings()
    assert int(rs.get("circuit_breaker_consec_losses", 0)) == 0  # never recorded


def test_record_close_records_circuit_breaker_for_mt5_trade(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555, net_pnl=0.0)
    engine._bridge = _FakeBridge(tick=None)
    db.update_risk_settings({"circuit_breaker_enabled": 1, "circuit_breaker_losses": 5})

    asyncio.run(SimulationEngine.record_close(engine, "t-1", 2380.0, "SL"))  # a loss

    rs = db.get_risk_settings()
    assert int(rs.get("circuit_breaker_consec_losses", 0)) == 1


# ── _close_all_ladder_legs ────────────────────────────────────────────────────

