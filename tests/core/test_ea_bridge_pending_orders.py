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
