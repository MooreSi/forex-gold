"""EABridge's pending-order surface (Limit Runner): place_pending_order()'s
wire format, the pending_order_placed/open_failed ack dispatch (reusing
open_trade()'s own _pending_open_acks mechanism), and the unsolicited
pending_order_filled/pending_order_cancelled event handlers -- the EA-side
counterpart lives in ForexTraderBridge.mq5's HandlePlacePendingOrder/
CheckPendingOrders, which this file cannot exercise directly (no MQL5
runtime here) but must stay in lockstep with."""
import asyncio
import json
import os
import tempfile
import time
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import ea_bridge


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
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


def _healthy_bridge():
    bridge = ea_bridge.EABridge(engine=None)
    bridge._writer = _FakeWriter()
    bridge._last_seen = time.time()
    return bridge


def _sent_message(bridge) -> dict:
    raw = bridge._writer.written[-1].decode().strip()
    return json.loads(raw)


def test_limit_runner_is_ea_portable():
    assert "limit_runner" in ea_bridge.EA_PORTABLE_STRATEGIES


def test_place_pending_order_wire_format():
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.place_pending_order(
            "trade-1", "BUY", 4148.0, 0.10, 4141.0,
            {1: 4151.0, 2: 4155.0, 3: 4160.0}, [0.25, 0.25, 0.25], 0, "limit_runner",
            expire_minutes=240.0, close_full_on_last=False, timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert msg["type"] == "place_pending_order"
    assert msg["direction"] == "BUY"
    assert msg["price"] == 4148.0
    assert msg["stop_loss"] == 4141.0
    assert msg["strategy"] == "limit_runner"
    assert msg["tp1"] == 4151.0 and msg["tp2"] == 4155.0 and msg["tp3"] == 4160.0
    assert "tp4" not in msg
    assert msg["pct1"] == 0.25 and msg["pct2"] == 0.25 and msg["pct3"] == 0.25
    assert msg["be_at_pos"] == 0
    assert msg["close_full_on_last"] == 0  # sent as int, not a native bool
    assert msg["expire_minutes"] == 240.0


def test_place_pending_order_defaults_close_full_on_last_true():
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.place_pending_order(
            "trade-1", "SELL", 4180.0, 0.10, 4189.0,
            {1: 4176.0}, [1.0], 0, "limit_runner", timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert msg["close_full_on_last"] == 1


def test_place_pending_order_raises_when_ea_unhealthy():
    bridge = ea_bridge.EABridge(engine=None)  # no writer -> unhealthy
    with pytest.raises(ConnectionError):
        asyncio.run(bridge.place_pending_order(
            "trade-1", "BUY", 4148.0, 0.10, 4141.0, {1: 4151.0}, [1.0], 0, "limit_runner",
        ))


def test_dispatch_routes_pending_order_placed_ack_to_the_waiting_call():
    bridge = _healthy_bridge()
    received = {}

    def _cb(payload):
        received.update(payload)

    bridge._pending_open_acks = {"trade-1": _cb}
    asyncio.run(bridge._dispatch({"type": "pending_order_placed", "trade_id": "trade-1", "ticket": 42}))
    assert received.get("ticket") == 42


def test_dispatch_routes_pending_order_open_failed_ack():
    bridge = _healthy_bridge()
    received = {}
    bridge._pending_open_acks = {"trade-1": received.update}
    asyncio.run(bridge._dispatch({
        "type": "pending_order_open_failed", "trade_id": "trade-1", "error": "Invalid stops",
    }))
    assert received.get("error") == "Invalid stops"


def _insert_pending_order(trade_id="t1", signal_id="s1", tp_open=1, strategy="limit_runner"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Telegram Auto (chan)", "BUY", 4148.0, 4148.0, 4141.0, 0.10, "pending", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_pending_orders (trade_id,signal_id,tg_message_id,channel_name,"
            "direction,price,stop_loss,tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,"
            "status,created_at,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, "tg1", "chan", "BUY", 4148.0, 4141.0,
             json.dumps({1: 4151.0, 2: 4155.0, 3: 4160.0}), json.dumps([0.25, 0.25, 0.25]),
             0, tp_open, 0.10, 999, "working", time.time(), strategy),
        )


def test_on_pending_order_filled_creates_managed_trade_row(fresh_db):
    _insert_pending_order()
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "t1", "ticket": 999, "fill_price": 4148.5,
    }))

    with db.db() as conn:
        row = conn.execute(
            "SELECT mt5_ticket,direction,entry_price,lot_size,stop_loss,tp1,tp2,tp3,strategy,"
            "managed_by,tp_open,status FROM vantage_simulated_trades WHERE trade_id='t1'"
        ).fetchone()
        assert tuple(row) == (999, "BUY", 4148.5, 0.10, 4141.0, 4151.0, 4155.0, 4160.0,
                              "limit_runner", "ea", 1, "open")

        sig_status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id='s1'"
        ).fetchone()[0]
        assert sig_status == "active"

        po_status = conn.execute(
            "SELECT status FROM vantage_pending_orders WHERE trade_id='t1'"
        ).fetchone()[0]
        assert po_status == "filled"

    assert bridge._active["t1"]["ticket"] == 999
    assert bridge._active["t1"]["strategy"] == "limit_runner"


def test_on_pending_order_filled_uses_the_strategy_stored_at_placement_time(fresh_db):
    """The strategy label must come from what was actually placed (e.g.
    orb_fixed), not be hardcoded to limit_runner -- a wrong label here would
    silently mismanage the trade once filled (ManageLadder instead of
    ManageOrbFixed, or vice versa)."""
    _insert_pending_order(strategy="orb_fixed")
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "t1", "ticket": 999, "fill_price": 4148.5,
    }))

    with db.db() as conn:
        strategy = conn.execute(
            "SELECT strategy FROM vantage_simulated_trades WHERE trade_id='t1'"
        ).fetchone()[0]
        assert strategy == "orb_fixed"

    assert bridge._active["t1"]["strategy"] == "orb_fixed"


def test_on_pending_order_filled_unknown_trade_id_is_a_noop(fresh_db):
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "does-not-exist", "ticket": 1, "fill_price": 1.0,
    }))  # must not raise
    assert bridge._active == {}


def _insert_grid_placeholder_trade(trade_id="grid1", strategy="template:GridVerify"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "Manual Market Order", "BUY", 4148.0, 4148.0, 4141.0, 0.10,
             "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,open_time,"
            "strategy,managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", 0, "BUY", 4148.0, 4148.0, 0.0, 0.10, 0.10, 4141.0,
             "open", time.time(), strategy, "ea"),
        )


def test_on_pending_order_filled_promotes_grid_leg_placeholder_row(fresh_db):
    """EA Template grid legs (HandleOpenTemplateGrid) never get a
    vantage_pending_orders row -- each leg only exists in the EA's own
    g_pending[], keyed "<original trade_id>-g<N>". The fill must promote
    the open_trade()-written placeholder row (mt5_ticket=0) in place
    instead of being silently dropped as an unknown trade_id."""
    _insert_grid_placeholder_trade()
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "grid1-g1", "ticket": 555, "fill_price": 4149.2,
    }))

    with db.db() as conn:
        row = conn.execute(
            "SELECT mt5_ticket,entry_price,strategy,status FROM vantage_simulated_trades "
            "WHERE trade_id='grid1'"
        ).fetchone()
        assert tuple(row) == (555, 4149.2, "template:GridVerify", "open")

    assert bridge._active["grid1"]["ticket"] == 555
    assert bridge._active["grid1"]["strategy"] == "template:GridVerify"


def test_on_pending_order_filled_second_grid_leg_is_noop_once_promoted(fresh_db):
    """cancel_pending=off could in principle let a second leg fill too --
    with the placeholder already consumed (mt5_ticket no longer 0), the
    second fill must not raise or clobber the first leg's real ticket."""
    _insert_grid_placeholder_trade()
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "grid1-g1", "ticket": 555, "fill_price": 4149.2,
    }))
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "grid1-g2", "ticket": 556, "fill_price": 4149.5,
    }))  # must not raise

    with db.db() as conn:
        ticket = conn.execute(
            "SELECT mt5_ticket FROM vantage_simulated_trades WHERE trade_id='grid1'"
        ).fetchone()[0]
        assert ticket == 555  # untouched by the second fill


def test_on_pending_order_filled_grid_leg_no_placeholder_row_is_noop(fresh_db):
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "nonexistent-g1", "ticket": 1, "fill_price": 1.0,
    }))  # must not raise
    assert bridge._active == {}


def test_on_pending_order_cancelled_marks_rows_cancelled(fresh_db):
    _insert_pending_order()
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_cancelled({"trade_id": "t1", "reason": "expired"}))

    with db.db() as conn:
        po_status = conn.execute(
            "SELECT status FROM vantage_pending_orders WHERE trade_id='t1'"
        ).fetchone()[0]
        assert po_status == "cancelled"

        sig_status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id='s1'"
        ).fetchone()[0]
        assert sig_status == "cancelled"

    with db.db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades WHERE trade_id='t1'"
        ).fetchone()[0]
        assert row == 0  # no trade was ever opened


# ── Trading Schedule gate on pending-order fills ──────────────────────────────
# A resting order is accepted by the broker before we can know whether the
# window's profit target will still allow it by the time it actually fills.
# Confirmed live 2026-07-23 that a hit target did not stop Limit Runner or EA
# Template grid fills -- the only protective action left once the fill has
# already happened is an immediate real close.

class _FakeEngine:
    def __init__(self):
        self.close_trade_calls: list[dict] = []

    async def close_trade(self, trade_id, reason):
        self.close_trade_calls.append({"trade_id": trade_id, "reason": reason})
        return {"trade_id": trade_id, "close_price": 0.0, "net_pnl": 0.0}


def test_limit_runner_fill_closed_when_schedule_blocked(fresh_db):
    _insert_pending_order(strategy="limit_runner")
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    with mock.patch.object(
        ea_bridge, "check_trading_schedule",
        return_value=(False, "profit target reached for this window"),
    ):
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "t1", "ticket": 999, "fill_price": 4148.5,
        }))
    assert engine.close_trade_calls == [{"trade_id": "t1", "reason": "trading_schedule_blocked"}]


def test_limit_runner_fill_not_closed_when_schedule_allows(fresh_db):
    _insert_pending_order(strategy="limit_runner")
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    with mock.patch.object(ea_bridge, "check_trading_schedule", return_value=(True, "")):
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "t1", "ticket": 999, "fill_price": 4148.5,
        }))
    assert engine.close_trade_calls == []


def test_orb_fixed_fill_exempt_from_schedule_gate(fresh_db):
    """ORB/IVB is deliberately exempt -- its own once-a-day dedup already
    caps volume, and orb_auto_execute() never reaches
    resolve_open_trade_params() by design (see core_orb_report.py)."""
    _insert_pending_order(strategy="orb_fixed")
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    with mock.patch.object(
        ea_bridge, "check_trading_schedule",
        return_value=(False, "profit target reached for this window"),
    ):
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "t1", "ticket": 999, "fill_price": 4148.5,
        }))
    assert engine.close_trade_calls == []
    assert bridge._active["t1"]["strategy"] == "orb_fixed"


def test_grid_leg_fill_closed_when_schedule_blocked(fresh_db):
    _insert_grid_placeholder_trade()
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    with mock.patch.object(
        ea_bridge, "check_trading_schedule",
        return_value=(False, "profit target reached for this window"),
    ):
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "grid1-g1", "ticket": 555, "fill_price": 4149.2,
        }))
    assert engine.close_trade_calls == [{"trade_id": "grid1", "reason": "trading_schedule_blocked"}]


def test_grid_leg_fill_not_closed_when_schedule_allows(fresh_db):
    _insert_grid_placeholder_trade()
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    with mock.patch.object(ea_bridge, "check_trading_schedule", return_value=(True, "")):
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "grid1-g1", "ticket": 555, "fill_price": 4149.2,
        }))
    assert engine.close_trade_calls == []
