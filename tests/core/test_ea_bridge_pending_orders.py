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

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_bridge


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


def test_on_pending_order_filled_carries_channel_and_pending_timing(fresh_db):
    """channel_name was known and stored on vantage_pending_orders at
    placement time but never carried over to the promoted trade row --
    confirmed live 2026-07-23 that every Limit Runner fill lost its real
    channel attribution (showed as an unattributed trade in Trade
    Analysis) and had no way to tell it apart from an immediate market
    open or see how long it sat pending before filling."""
    placed_at = time.time() - 42.0
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("s2", "Telegram Auto (Gold Diggers VIP)", "BUY", 4148.0, 4148.0, 4141.0, 0.10,
             "pending", placed_at),
        )
        conn.execute(
            "INSERT INTO vantage_pending_orders (trade_id,signal_id,tg_message_id,channel_name,"
            "direction,price,stop_loss,tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,"
            "status,created_at,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t2", "s2", "tg2", "Gold Diggers VIP", "BUY", 4148.0, 4141.0,
             json.dumps({1: 4151.0}), json.dumps([1.0]), 0, 0, 0.10, 998, "working",
             placed_at, "limit_runner"),
        )
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "t2", "ticket": 998, "fill_price": 4148.5,
    }))

    with db.db() as conn:
        row = conn.execute(
            "SELECT tg_source,order_type,pending_placed_at FROM vantage_simulated_trades "
            "WHERE trade_id='t2'"
        ).fetchone()
    assert row[0] == "Gold Diggers VIP"
    assert row[1] == "limit"
    assert row[2] == placed_at


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


def _insert_grid_placeholder_trade(trade_id="grid1", strategy="template:GridVerify", open_time=None):
    open_time = open_time if open_time is not None else time.time()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "Manual Market Order", "BUY", 4148.0, 4148.0, 4141.0, 0.10,
             "active", open_time),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,open_time,"
            "strategy,managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", 0, "BUY", 4148.0, 4148.0, 0.0, 0.10, 0.10, 4141.0,
             "open", open_time, strategy, "ea"),
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


def test_grid_leg_fill_records_order_type_and_pending_placed_at(fresh_db):
    """The placeholder row's own open_time (when open_trade() placed the
    grid legs) is the only placement timestamp that exists for a leg --
    grid legs never get their own vantage_pending_orders row -- so it must
    be captured before the UPDATE overwrites open_time to the fill time."""
    placed_at = time.time() - 17.0
    _insert_grid_placeholder_trade(open_time=placed_at)
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_filled({
        "trade_id": "grid1-g1", "ticket": 555, "fill_price": 4149.2,
    }))

    with db.db() as conn:
        row = conn.execute(
            "SELECT order_type,pending_placed_at FROM vantage_simulated_trades WHERE trade_id='grid1'"
        ).fetchone()
    assert row[0] == "limit"
    assert row[1] == placed_at


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


def test_grid_leg_fill_telegram_alert_has_full_trade_detail(fresh_db):
    """The grid-leg-fill alert must carry the same detail as a normal
    trade-opened message (ticket, strategy, channel, SL/TP, entry) --
    replacing the old bare one-liner ("EA Template grid leg FILLED —
    {dir} {lot} lots @ {price} (ticket {ticket})") that gave no way to
    tell which channel or strategy a fill belonged to without cross-
    referencing the DB."""
    _insert_grid_placeholder_trade(trade_id="grid1", strategy="template:Sig Gen Grid")
    with db.db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET tg_source='Reversal Engine', "
            "tp1=4155.0, tp2=4160.0 WHERE trade_id='grid1'"
        )
    bridge = ea_bridge.EABridge(engine=None)
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    with mock.patch("backend.src.services.telegram.alerts.send_message", side_effect=_capture):
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "grid1-g1", "ticket": 555, "fill_price": 4149.2,
        }))
        asyncio.run(asyncio.sleep(0))

    assert len(sent) == 1
    body = sent[0]
    assert "Grid Leg 1" in body
    assert "Trade Opened" in body
    assert "555" in body           # MT5 ticket
    assert "Sig Gen Grid" in body  # strategy
    assert "Reversal Engine" in body  # channel
    assert "4149.2" in body        # fill price
    assert "4155.0" in body        # TP1
    assert "additional leg" not in body.lower()


def test_second_grid_leg_fill_now_sends_its_own_alert(fresh_db):
    """Regression: with cancel_pending off, a second leg's real broker
    fill was previously dropped entirely -- a warning log line, no
    Telegram alert, even though the position genuinely exists. It cannot
    be promoted into the DB (only one row per template trade), but its
    fill must still be reported, clearly marked as not DB-tracked."""
    _insert_grid_placeholder_trade(trade_id="grid1", strategy="template:Sig Gen Grid")
    bridge = ea_bridge.EABridge(engine=None)
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    with mock.patch("backend.src.services.telegram.alerts.send_message", side_effect=_capture):
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "grid1-g1", "ticket": 555, "fill_price": 4149.2,
        }))
        asyncio.run(bridge._on_pending_order_filled({
            "trade_id": "grid1-g2", "ticket": 556, "fill_price": 4149.5,
        }))
        asyncio.run(asyncio.sleep(0))

    assert len(sent) == 2
    assert "556" in sent[1]
    assert "additional leg" in sent[1].lower()
    with db.db() as conn:
        ticket = conn.execute(
            "SELECT mt5_ticket FROM vantage_simulated_trades WHERE trade_id='grid1'"
        ).fetchone()[0]
    assert ticket == 555  # still untouched by the second fill


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


# ── Restoring resting orders to a freshly (re)connected EA ────────────────────
# g_pending[] is pure in-memory state on the EA side with no persistence of
# its own -- any EA restart silently forgets every order still resting,
# permanently orphaning it from EA-side fill/expiry detection. Confirmed live
# 2026-07-24: 5 Limit Runner orders sat "pending" in the UI 16+ hours after
# genuinely expiring on MT5. Python pushes every still-"working" row back to
# the EA the moment a fresh "hello" arrives.

def test_restore_pending_order_wire_format():
    bridge = _healthy_bridge()
    row = {
        "trade_id": "t1", "ea_ticket": 999, "direction": "BUY",
        "lot_size": 0.10, "stop_loss": 4141.0, "strategy": "limit_runner",
        "be_at_pos": 0, "tp_open": 1,
        "tps_json": json.dumps({1: 4151.0, 2: 4155.0, 3: 4160.0}),
        "pcts_json": json.dumps([0.25, 0.25, 0.25]),
    }
    asyncio.run(bridge.restore_pending_order(row))
    sent = _sent_message(bridge)
    assert sent["type"] == "restore_pending_order"
    assert sent["trade_id"] == "t1"
    assert sent["ticket"] == 999
    assert sent["direction"] == "BUY"
    assert sent["lot_size"] == 0.10
    assert sent["stop_loss"] == 4141.0
    assert sent["strategy"] == "limit_runner"
    assert sent["be_at_pos"] == 0
    assert sent["close_full_on_last"] == 0  # tp_open=1 -> last TP doesn't close everything
    assert sent["tp1"] == 4151.0 and sent["tp2"] == 4155.0 and sent["tp3"] == 4160.0
    assert sent["pct1"] == 0.25 and sent["pct2"] == 0.25 and sent["pct3"] == 0.25


def test_restore_pending_order_close_full_on_last_when_no_tp_open():
    bridge = _healthy_bridge()
    row = {
        "trade_id": "t2", "ea_ticket": 1000, "direction": "SELL",
        "lot_size": 0.10, "stop_loss": 4160.0, "strategy": "orb_fixed",
        "be_at_pos": 0, "tp_open": 0,
        "tps_json": json.dumps({1: 4150.0}),
        "pcts_json": json.dumps([1.0]),
    }
    asyncio.run(bridge.restore_pending_order(row))
    assert _sent_message(bridge)["close_full_on_last"] == 1


def test_restore_pending_orders_pushes_every_still_working_row(fresh_db):
    _insert_pending_order(trade_id="t1", signal_id="s1")
    _insert_pending_order(trade_id="t2", signal_id="s2")
    with db.db() as conn:
        conn.execute("UPDATE vantage_pending_orders SET status='filled' WHERE trade_id='t2'")

    bridge = _healthy_bridge()
    asyncio.run(bridge._restore_pending_orders())

    sent_trade_ids = [json.loads(w.decode())["trade_id"] for w in bridge._writer.written]
    assert sent_trade_ids == ["t1"]  # only the still-'working' row


def test_dispatch_hello_schedules_restore():
    bridge = _healthy_bridge()
    calls = []

    async def _fake_restore():
        calls.append(True)

    bridge._restore_pending_orders = _fake_restore

    async def _run():
        await bridge._dispatch({"type": "hello", "account": 1, "symbol": "XAUUSD"})
        await asyncio.sleep(0)  # let the scheduled task run

    asyncio.run(_run())
    assert calls == [True]


# ── Global Parameters > Harvest (2026-07-24) ───────────────────────────────────

def test_push_global_config_wire_format_enabled(fresh_db):
    db.update_risk_settings({
        "global_harvest_enabled": 1, "global_harvest_threshold_usd": 75.0,
    })
    bridge = _healthy_bridge()
    asyncio.run(bridge.push_global_config())
    msg = _sent_message(bridge)
    assert msg["type"] == "set_global_config"
    assert msg["harvest_enabled"] == 1
    assert msg["harvest_threshold"] == 75.0


def test_push_global_config_wire_format_disabled_default(fresh_db):
    bridge = _healthy_bridge()
    asyncio.run(bridge.push_global_config())
    msg = _sent_message(bridge)
    assert msg["harvest_enabled"] == 0
    assert msg["harvest_threshold"] == 50.0


def test_dispatch_hello_schedules_push_global_config():
    bridge = _healthy_bridge()
    calls = []

    async def _fake_push():
        calls.append(True)

    bridge.push_global_config = _fake_push
    bridge._restore_pending_orders = lambda: asyncio.sleep(0)

    async def _run():
        await bridge._dispatch({"type": "hello", "account": 1, "symbol": "XAUUSD"})
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert calls == [True]


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
