"""Proves backend.src.services.trading.close_trade's extracted functions behave
identically to the SimulationEngine methods characterized in
test_close_trade_characterization.py -- see
docs/todo/refactor/core-close-trade-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. The SimulationEngine.__new__() instance + loose _bridge/_tp_cache/
etc attributes are replaced by a CloseTradeContext. NO real or demo MT5
order is ever placed, closed, or modified -- verified via the fake
bridge's own call log, same as 010.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from forex_trader.core import database as db
from backend.src.services.trading import close_trade as ct


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
def ctx(fresh_db):
    return ct.CloseTradeContext(_FakeBridge(), starting_balance=1000.0)


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


# ── get_trading_balance ────────────────────────────────────────────────────────

def test_get_trading_balance_prefers_live_mt5_balance(fresh_db):
    bridge = _FakeBridge(account={"balance": 1234.5})
    balance = asyncio.run(ct.get_trading_balance(bridge, 1000.0))
    assert balance == 1234.5


def test_get_trading_balance_falls_back_to_sim_account(fresh_db):
    bridge = _FakeBridge(account={"balance": 0})
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulation_account SET balance=? WHERE id=1", (777.0,))
    balance = asyncio.run(ct.get_trading_balance(bridge, 1000.0))
    assert balance == 777.0


# ── close_trade ────────────────────────────────────────────────────────────────

def test_close_trade_raises_when_not_open(fresh_db, ctx):
    _insert_signal()
    _insert_trade("t-1", status="closed")
    with pytest.raises(ValueError):
        asyncio.run(ct.close_trade("t-1", "manual_close", ctx))


def test_close_trade_uses_tick_bid_for_buy_close_price(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    ctx = ct.CloseTradeContext(_FakeBridge(tick=_tick(bid=2415.0, ask=2415.5)), starting_balance=1000.0)
    result = asyncio.run(ct.close_trade("t-1", "manual_close", ctx))
    assert result["close_price"] == 2415.0


def test_close_trade_falls_back_to_entry_price_with_no_tick_no_ticket(fresh_db, ctx):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    result = asyncio.run(ct.close_trade("t-1", "manual_close", ctx))
    assert result["close_price"] == 2400.0


def test_close_trade_calls_bridge_close_position_for_mt5_ticket(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555)
    bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={555: {"success": True, "close_price": 2416.0}},
    )
    ctx = ct.CloseTradeContext(bridge, starting_balance=1000.0)
    result = asyncio.run(ct.close_trade("t-1", "manual_close", ctx))
    assert bridge.close_position_calls == [555]
    assert result["close_price"] == 2416.0


def test_close_trade_raises_on_bridge_error(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555)
    bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={555: {"error": "no such ticket"}},
    )
    ctx = ct.CloseTradeContext(bridge, starting_balance=1000.0)
    with pytest.raises(RuntimeError):
        asyncio.run(ct.close_trade("t-1", "manual_close", ctx))


def test_close_trade_raises_on_bridge_success_false(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555)
    bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={555: {"success": False}},
    )
    ctx = ct.CloseTradeContext(bridge, starting_balance=1000.0)
    with pytest.raises(RuntimeError):
        asyncio.run(ct.close_trade("t-1", "manual_close", ctx))


def test_close_trade_routes_to_ladder_legs_when_any_leg_open(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=100)
    _insert_ladder_leg("t-1", tier=1, tp_num=1, tp_price=2410.0, lots=0.05, mt5_ticket=101, status="open")
    _insert_ladder_leg("t-1", tier=2, tp_num=2, tp_price=2420.0, lots=0.05, mt5_ticket=102, status="closed")
    bridge = _FakeBridge(
        tick=_tick(bid=2415.0, ask=2415.5),
        close_results={101: {"success": True, "close_price": 2412.0}},
    )
    ctx = ct.CloseTradeContext(bridge, starting_balance=1000.0)
    asyncio.run(ct.close_trade("t-1", "manual_close", ctx))
    assert bridge.close_position_calls == [101]
    trade = _get_trade("t-1")
    assert trade["status"] == "closed"


# ── record_close ───────────────────────────────────────────────────────────────

def test_record_close_updates_balance_and_cascades_signal(fresh_db, ctx):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", direction="BUY", mt5_ticket=None)
    result = asyncio.run(ct.record_close("t-1", 2410.0, "manual_close", ctx))

    with db.db() as conn:
        balance = conn.execute("SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0]
    assert balance == round(1000.0 + result["net_pnl"], 4)

    with db.db() as conn:
        sig_status = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-1",)).fetchone()[0]
    assert sig_status == "closed"


def test_record_close_invalidates_caches(fresh_db, ctx):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    ctx.tp_cache.triggered["t-1"] = ({1}, time.time())
    ctx.scale_out_last_fail[("t-1", 1)] = time.time()
    ctx.tp_safety_net_last_alert["t-1"] = time.time()

    asyncio.run(ct.record_close("t-1", 2410.0, "manual_close", ctx))

    assert "t-1" not in ctx.tp_cache.triggered
    assert ("t-1", 1) not in ctx.scale_out_last_fail
    assert "t-1" not in ctx.tp_safety_net_last_alert


def test_record_close_updates_peak_balance_when_higher(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    ctx = ct.CloseTradeContext(_FakeBridge(account={"balance": 5000.0}), starting_balance=1000.0)
    db.set_app_config("peak_balance", "1000.0")

    asyncio.run(ct.record_close("t-1", 2410.0, "manual_close", ctx))

    assert float(db.get_app_config("peak_balance")) == 5000.0


def test_record_close_does_not_lower_peak_balance(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    ctx = ct.CloseTradeContext(_FakeBridge(account={"balance": 100.0}), starting_balance=1000.0)
    db.set_app_config("peak_balance", "9000.0")

    asyncio.run(ct.record_close("t-1", 2410.0, "manual_close", ctx))

    assert float(db.get_app_config("peak_balance")) == 9000.0


def test_record_close_skips_risk_governor_when_disabled(fresh_db, ctx):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    db.update_risk_settings({"risk_governor_enabled": 0})
    db.set_app_config("trade_pause_until", "")

    asyncio.run(ct.record_close("t-1", 2410.0, "manual_close", ctx))

    assert db.get_app_config("trade_pause_until") in (None, "")


def test_record_close_runs_risk_governor_when_enabled(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    ctx = ct.CloseTradeContext(_FakeBridge(account={"balance": 100.0}), starting_balance=1000.0)
    db.update_risk_settings({
        "risk_governor_enabled": 1,
        "max_total_drawdown_pct": 5.0,
    })
    db.set_app_config("peak_balance", "10000.0")

    asyncio.run(ct.record_close("t-1", 2410.0, "manual_close", ctx))

    assert db.get_app_config("trade_pause_until") is not None


def test_record_close_skips_circuit_breaker_for_non_mt5_trade(fresh_db, ctx):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=None)
    db.update_risk_settings({"circuit_breaker_enabled": 1, "circuit_breaker_losses": 1})

    asyncio.run(ct.record_close("t-1", 2380.0, "SL", ctx))

    rs = db.get_risk_settings()
    assert int(rs.get("circuit_breaker_consec_losses", 0)) == 0


def test_record_close_records_circuit_breaker_for_mt5_trade(fresh_db, ctx):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", mt5_ticket=555, net_pnl=0.0)
    db.update_risk_settings({"circuit_breaker_enabled": 1, "circuit_breaker_losses": 5})

    asyncio.run(ct.record_close("t-1", 2380.0, "SL", ctx))

    rs = db.get_risk_settings()
    assert int(rs.get("circuit_breaker_consec_losses", 0)) == 1


# ── close_all_ladder_legs ──────────────────────────────────────────────────────

def test_close_all_ladder_legs_sums_pnl_and_skips_rejected(fresh_db):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", direction="BUY", mt5_ticket=100)
    row = _get_trade("t-1")
    legs = [
        {"id": 1, "tier": 1, "status": "open", "mt5_ticket": 101, "entry_price": 2400.0,
         "lots": 0.05, "tp_price": 2410.0},
        {"id": 2, "tier": 2, "status": "open", "mt5_ticket": 102, "entry_price": 2400.0,
         "lots": 0.05, "tp_price": 2420.0},
        {"id": 3, "tier": 3, "status": "closed", "mt5_ticket": 103, "entry_price": 2400.0,
         "lots": 0.05, "tp_price": 2430.0},
    ]
    for leg in legs:
        _insert_ladder_leg("t-1", leg["tier"], leg["tier"], leg["tp_price"], leg["lots"],
                           leg["mt5_ticket"], status=leg["status"], entry_price=leg["entry_price"])

    bridge = _FakeBridge(close_results={
        101: {"success": True, "close_price": 2411.0},
        102: {"success": False, "error": "rejected"},
    })
    ctx = ct.CloseTradeContext(bridge, starting_balance=1000.0)
    result = asyncio.run(ct.close_all_ladder_legs("t-1", row, legs, "manual_close", ctx))

    assert bridge.close_position_calls == [101, 102]
    assert result["gross_pnl"] != 0

    trade = _get_trade("t-1")
    assert trade["status"] == "closed"
