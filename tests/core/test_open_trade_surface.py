"""Proves forex_trader.core.core_open_trade's extracted function behaves
identically to SimulationEngine.open_trade, characterized in
test_open_trade_characterization.py -- see
docs/todo/refactor/core-open-trade-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, via the Python bridge OR
the EA bridge, at any point in this file -- verified via each fake's own
call log, same as 010.
"""
import asyncio
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from forex_trader.core import database as db
from forex_trader.core import ea_bridge
from forex_trader.core import core_open_trade as ot
from forex_trader.core.models import STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER


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
    def __init__(self, tick="default", order_result=None):
        self._tick = _default_tick() if tick == "default" else tick
        self._order_result = order_result or {"ticket": 999, "fill_price": 2400.5}
        self.place_order_calls = []

    async def get_fresh_tick(self):
        return self._tick

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.place_order_calls.append(
            {"direction": direction, "lots": lots, "sl": sl, "tp": tp, "comment": comment}
        )
        return self._order_result


def _default_tick():
    from types import SimpleNamespace
    return SimpleNamespace(bid=2399.8, ask=2400.2)


class _FakeEA:
    def __init__(self, healthy=True, portable=True, ack=None):
        self._healthy = healthy
        self._portable = portable
        self._ack = ack or {"type": "trade_opened", "ticket": 8001, "fill_price": 2400.7}
        self.open_trade_calls = []

    def is_ea_healthy(self):
        return self._healthy

    def is_strategy_portable(self, strategy):
        return self._portable

    async def open_trade(self, trade_id, direction, lot_size, stop_loss, tps, strategy,
                         pcts=None, be_at_pos=None, trail_mode=None, template=None):
        self.open_trade_calls.append(
            {"trade_id": trade_id, "direction": direction, "lot_size": lot_size,
             "stop_loss": stop_loss, "tps": tps, "strategy": strategy}
        )
        return self._ack


class _FakeSyncClient:
    def __init__(self, ack=None):
        self._ack = ack or {"type": "signal_order_ack", "result": {"trade_id": "remote-1", "mt5_ticket": 777}}
        self.send_signal_order_calls = []

    async def send_signal_order(self, **kwargs):
        self.send_signal_order_calls.append(kwargs)
        return self._ack


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "pending", time.time()),
        )


def _open_kwargs(**overrides):
    kw = dict(
        signal_id="sig-1", direction="BUY", entry_low=2399.0, entry_high=2401.0,
        stop_loss=2390.0, tp1=2410.0, lot_size=0.10, strategy=STRATEGY_SCALE_OUT,
    )
    kw.update(overrides)
    return kw


# ── happy path ─────────────────────────────────────────────────────────────────

def test_happy_path_places_order_and_inserts_trade(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    result = asyncio.run(ot.open_trade(bridge, **_open_kwargs()))

    assert result["managed_by"] == "python"
    assert result["mt5_ticket"] == 999
    assert bridge.place_order_calls == [
        {"direction": "BUY", "lots": 0.10, "sl": 2390.0, "tp": None, "comment": "sig:sig-1"}
    ]
    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (result["trade_id"],)).fetchone()
        )
    assert row is not None
    assert row["status"] == "open"
    with db.db() as conn:
        sig_status = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-1",)).fetchone()[0]
    assert sig_status == "active"


# ── gates ──────────────────────────────────────────────────────────────────────

def test_raises_when_trading_paused(fresh_db):
    _insert_signal()
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="paused"):
        asyncio.run(ot.open_trade(bridge, **_open_kwargs()))
    assert bridge.place_order_calls == []


def test_raises_when_circuit_breaker_active(fresh_db):
    _insert_signal()
    db.update_risk_settings({
        "circuit_breaker_enabled": 1,
        "circuit_breaker_active_until": time.time() + 3600,
    })
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="circuit breaker"):
        asyncio.run(ot.open_trade(bridge, **_open_kwargs()))
    assert bridge.place_order_calls == []


def test_raises_when_max_open_trades_reached(fresh_db):
    _insert_signal("sig-0")
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, entry_low, "
            "entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t-existing", "sig-0", "BUY", 2399.0, 2401.0, 2400.0, 0.10, 0.10, 2390.0,
             "open", time.time()),
        )
    _insert_signal("sig-1")
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Max open trades"):
        asyncio.run(ot.open_trade(bridge, **_open_kwargs(signal_id="sig-1")))
    assert bridge.place_order_calls == []


def test_raises_when_no_live_tick(fresh_db):
    _insert_signal()
    bridge = _FakeBridge(tick=None)
    with pytest.raises(RuntimeError, match="No live price"):
        asyncio.run(ot.open_trade(bridge, **_open_kwargs()))


def test_raises_and_inserts_nothing_when_bridge_rejects(fresh_db):
    _insert_signal()
    bridge = _FakeBridge(order_result={"error": "requote"})
    with pytest.raises(RuntimeError, match="rejected"):
        asyncio.run(ot.open_trade(bridge, **_open_kwargs()))
    with db.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
    assert count == 0


# ── EA handoff ─────────────────────────────────────────────────────────────────

def test_ea_managed_path_used_when_healthy_and_portable(fresh_db):
    _insert_signal()
    db.update_risk_settings({"ea_bridge_enabled": 1})
    fake_ea = _FakeEA(healthy=True, portable=True)
    ea_bridge.set_instance(fake_ea)
    bridge = _FakeBridge()

    result = asyncio.run(ot.open_trade(bridge, **_open_kwargs()))

    assert result["managed_by"] == "ea"
    assert result["mt5_ticket"] == 8001
    assert len(fake_ea.open_trade_calls) == 1
    assert bridge.place_order_calls == []


def test_falls_through_to_python_bridge_when_no_ea_instance(fresh_db):
    _insert_signal()
    db.update_risk_settings({"ea_bridge_enabled": 1})
    ea_bridge.set_instance(None)
    bridge = _FakeBridge()

    result = asyncio.run(ot.open_trade(bridge, **_open_kwargs()))

    assert result["managed_by"] == "python"
    assert len(bridge.place_order_calls) == 1


def test_falls_through_to_python_bridge_when_ea_unhealthy(fresh_db):
    _insert_signal()
    db.update_risk_settings({"ea_bridge_enabled": 1})
    ea_bridge.set_instance(_FakeEA(healthy=False))
    bridge = _FakeBridge()

    result = asyncio.run(ot.open_trade(bridge, **_open_kwargs()))

    assert result["managed_by"] == "python"


def test_falls_through_to_python_bridge_when_strategy_not_portable(fresh_db):
    _insert_signal()
    db.update_risk_settings({"ea_bridge_enabled": 1})
    ea_bridge.set_instance(_FakeEA(healthy=True, portable=False))
    bridge = _FakeBridge()

    result = asyncio.run(ot.open_trade(bridge, **_open_kwargs()))

    assert result["managed_by"] == "python"


# ── EA Templates -- no Python-bridge fallback ────────────────────────────────

def test_template_strategy_ea_managed_forwards_template_payload(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Grid Stealth", {"mode": "grid", "tpsl_mode": "stealth"})
    _insert_signal()
    db.update_risk_settings({"ea_bridge_enabled": 1})
    fake_ea = _FakeEA(healthy=True, portable=True)
    ea_bridge.set_instance(fake_ea)
    bridge = _FakeBridge()

    result = asyncio.run(ot.open_trade(
        bridge, **_open_kwargs(strategy=et.override_for_template("Grid Stealth"))
    ))

    assert result["managed_by"] == "ea"
    assert len(fake_ea.open_trade_calls) == 1


def test_template_strategy_raises_when_ea_unhealthy_no_python_fallback(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Grid Stealth", {})
    _insert_signal()
    db.update_risk_settings({"ea_bridge_enabled": 1})
    ea_bridge.set_instance(_FakeEA(healthy=False))
    bridge = _FakeBridge()

    with pytest.raises(RuntimeError):
        asyncio.run(ot.open_trade(
            bridge, **_open_kwargs(strategy=et.override_for_template("Grid Stealth"))
        ))
    assert bridge.place_order_calls == []


def test_template_strategy_raises_when_ea_bridge_disabled_no_python_fallback(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Grid Stealth", {})
    _insert_signal()
    db.update_risk_settings({"ea_bridge_enabled": 0})
    bridge = _FakeBridge()

    with pytest.raises(RuntimeError):
        asyncio.run(ot.open_trade(
            bridge, **_open_kwargs(strategy=et.override_for_template("Grid Stealth"))
        ))
    assert bridge.place_order_calls == []


# ── TP selection for the broker-side order ─────────────────────────────────────

def test_mt5_tp_override_used_verbatim(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    asyncio.run(ot.open_trade(bridge, **_open_kwargs(mt5_tp_override=2450.0)))
    assert bridge.place_order_calls[0]["tp"] == 2450.0


def test_be_runner_sends_highest_populated_tp(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    asyncio.run(ot.open_trade(
        bridge, **_open_kwargs(strategy=STRATEGY_BE_RUNNER, tp1=2410.0, tp2=2420.0, tp3=None)
    ))
    assert bridge.place_order_calls[0]["tp"] == 2420.0


def test_scale_out_sends_no_broker_side_tp(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    asyncio.run(ot.open_trade(bridge, **_open_kwargs(strategy=STRATEGY_SCALE_OUT, tp1=2410.0)))
    assert bridge.place_order_calls[0]["tp"] is None


# ── remote-node forwarding ───────────────────────────────────────────────────────

def test_remote_forwarding_raises_stood_down_when_not_centralized(fresh_db):
    _insert_signal()
    db.set_app_config("sync_remote_host", "10.0.0.5")
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="stood down"):
        asyncio.run(ot.open_trade(bridge, **_open_kwargs()))
    assert bridge.place_order_calls == []


def test_remote_forwarding_forwards_when_centralized(fresh_db):
    _insert_signal()
    db.set_app_config("sync_remote_host", "10.0.0.5")
    db.update_risk_settings({"centralized_signal_gen_enabled": 1})
    fake_client = _FakeSyncClient()
    bridge = _FakeBridge()

    with patch("forex_trader.sync.client.get_instance", return_value=fake_client):
        result = asyncio.run(ot.open_trade(bridge, **_open_kwargs()))

    assert result["executed_remotely"] is True
    assert result["trade_id"] == "remote-1"
    assert len(fake_client.send_signal_order_calls) == 1
    assert bridge.place_order_calls == []
