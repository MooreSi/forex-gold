"""Proves forex_trader.core.core_mt5_position_sync's extracted function
behaves identically to SimulationEngine._sync_closed_mt5_positions,
characterized in test_mt5_position_sync_characterization.py -- see
docs/todo/refactor/core-mt5-position-sync-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import telegram_alerts
from forex_trader.core import core_mt5_position_sync as sync


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
    def __init__(self, configured=True, positions=None, health=None,
                deal_history=None, position_history=None):
        self._configured = configured
        self._positions = positions if positions is not None else []
        self._health = health or {"connected": True}
        self._deal_history = deal_history or []
        self._position_history = position_history

    def is_configured(self):
        return self._configured

    async def get_positions(self):
        return self._positions

    async def get_health(self):
        return self._health

    async def get_deal_history(self, days):
        return self._deal_history

    async def get_position_history(self, ticket):
        return self._position_history

    async def get_account(self):
        return {"balance": 1000.0}

    async def get_tick(self):
        return SimpleNamespace(bid=2400.0, ask=2400.5)


def _insert_trade(trade_id="t-1", mt5_ticket=555, remaining_lots=0.10,
                  direction="BUY", managed_by="python", status="open"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, 2400.0, 2400.0, 2390.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, direction, 2400.0, 2400.0, 2400.0,
             remaining_lots, remaining_lots, 2390.0, status, time.time(), managed_by),
        )


def _trade_dict(trade_id="t-1"):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_bridge_not_configured_returns_immediately(fresh_db):
    asyncio.run(sync.sync_closed_mt5_positions(_FakeBridge(configured=False), {}))


def test_no_open_trades_skips_import_pass_too(fresh_db):
    bridge = _FakeBridge(positions=[{"ticket": 777, "type": "BUY", "volume": 0.05, "open_price": 2400.0}])
    asyncio.run(sync.sync_closed_mt5_positions(bridge, {}))
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
    assert n == 0


def test_ticket_still_live_clears_streak(fresh_db):
    _insert_trade(mt5_ticket=555)
    bridge = _FakeBridge(positions=[{"ticket": 555, "volume": 0.10}])
    streak = {"t-1": 1}
    asyncio.run(sync.sync_closed_mt5_positions(bridge, streak))
    assert "t-1" not in streak


def test_disconnected_bridge_empty_positions_skips_whole_sync(fresh_db):
    _insert_trade(mt5_ticket=555)
    bridge = _FakeBridge(positions=[], health={"connected": False})
    with mock.patch.object(sync, "record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(sync.sync_closed_mt5_positions(bridge, {}))
    assert not rc.called


def test_miss_streak_below_threshold_not_yet_closed(fresh_db):
    _insert_trade(mt5_ticket=555)
    bridge = _FakeBridge(positions=[])
    streak = {}
    with mock.patch.object(sync, "record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(sync.sync_closed_mt5_positions(bridge, streak))
    assert not rc.called
    assert streak["t-1"] == 1


def test_miss_streak_reaches_threshold_full_close_fallback_price(fresh_db):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(positions=[], deal_history=[], position_history=[])
    streak = {"t-1": 1}
    with mock.patch.object(sync, "record_close", new=mock.AsyncMock(return_value={"net_pnl": 0})) as rc, \
         mock.patch.object(sync, "sync_profit", new=mock.AsyncMock()), \
         mock.patch.object(sync, "schedule_profit_sync", new=mock.AsyncMock()), \
         mock.patch.object(telegram_alerts, "fmt_trade_close", return_value="msg"), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(sync.sync_closed_mt5_positions(bridge, streak))
    assert rc.call_args.args[0] == "t-1"
    assert rc.call_args.args[1] == 2400.0
    assert rc.call_args.args[2] == "MT5_close"


def test_full_close_infers_sl_reason_from_comment(fresh_db):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    deals = [{"entry": 1, "position_id": 555, "price": 2390.0, "time": 100,
             "comment": "sl 2390", "volume": 0.10, "profit": -10.0}]
    bridge = _FakeBridge(positions=[], deal_history=[], position_history=deals)
    streak = {"t-1": 1}
    with mock.patch.object(sync, "record_close", new=mock.AsyncMock(return_value={"net_pnl": -10})) as rc, \
         mock.patch.object(sync, "sync_profit", new=mock.AsyncMock()), \
         mock.patch.object(sync, "schedule_profit_sync", new=mock.AsyncMock()), \
         mock.patch.object(telegram_alerts, "fmt_trade_close", return_value="msg"), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(sync.sync_closed_mt5_positions(bridge, streak))
    assert rc.call_args.args[0] == "t-1"
    assert rc.call_args.args[1] == 2390.0
    assert rc.call_args.args[2] == "SL"


def test_partial_close_detected_no_full_close_reason_not_double_prefixed(fresh_db):
    # Regression (2026-07-27): `reason` is already "MT5_close"/"MT5_sync_TP"
    # in two of its three possible values (only "SL" isn't), so blindly
    # prefixing with f"MT5_{reason}" produced "MT5_MT5_close"/
    # "MT5_MT5_sync_TP" -- found investigating a live incident where this
    # same value ended up in vantage_partial_closes.reason.
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    partial_deals = [{"entry": 1, "position_id": 555, "price": 2405.0, "time": 100,
                      "comment": "tp", "volume": 0.05, "profit": 5.0, "swap": 0, "fee": 0}]
    bridge = _FakeBridge(positions=[{"ticket": 999, "volume": 0.05}],
                         deal_history=[], position_history=partial_deals)
    streak = {"t-1": 1}
    with mock.patch.object(sync, "partial_close_trade",
                           new=mock.AsyncMock(return_value={"partial_pnl": 5.0})) as pc, \
         mock.patch.object(sync, "record_close", new=mock.AsyncMock()) as rc, \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(sync.sync_closed_mt5_positions(bridge, streak))
    pc.assert_called_once_with("t-1", 0.05, 2405.0, "MT5_sync_TP")
    assert not rc.called
    assert "t-1" not in streak


def test_partial_close_sl_reason_still_gets_prefixed(fresh_db):
    # "SL" (unlike "MT5_close"/"MT5_sync_TP") does NOT already start with
    # "MT5_", so it must still be prefixed to "MT5_SL".
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    partial_deals = [{"entry": 1, "position_id": 555, "price": 2390.0, "time": 100,
                      "comment": "sl hit", "volume": 0.05, "profit": -5.0, "swap": 0, "fee": 0}]
    bridge = _FakeBridge(positions=[{"ticket": 999, "volume": 0.05}],
                         deal_history=[], position_history=partial_deals)
    with mock.patch.object(sync, "partial_close_trade",
                           new=mock.AsyncMock(return_value={"partial_pnl": -5.0})) as pc, \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(sync.sync_closed_mt5_positions(bridge, {"t-1": 1}))
    pc.assert_called_once_with("t-1", 0.05, 2390.0, "MT5_SL")


def test_partial_close_reassigns_ticket_when_continuing_position_found(fresh_db):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    partial_deals = [{"entry": 1, "position_id": 555, "price": 2405.0, "time": 100,
                      "comment": "tp", "volume": 0.05, "profit": 5.0, "swap": 0, "fee": 0}]
    bridge = _FakeBridge(positions=[{"ticket": 999, "volume": 0.05}],
                         deal_history=[], position_history=partial_deals)
    with mock.patch.object(sync, "partial_close_trade",
                           new=mock.AsyncMock(return_value={"partial_pnl": 5.0})), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(sync.sync_closed_mt5_positions(bridge, {"t-1": 1}))
    assert _trade_dict()["mt5_ticket"] == 999


def test_ladder_leg_trade_excluded_from_close_detection(fresh_db):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-parent", "BUY", 2400.0, 2400.0, 2390.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trade-parent", "sig-parent", 900, "BUY", 2400.0, 2400.0, 2400.0, 0.10, 0.10,
             2390.0, "open", time.time(), "python"),
        )
        conn.execute(
            "INSERT INTO vantage_ladder_legs (trade_id, tier, tp_num, tp_price, lots, "
            "mt5_ticket, status) VALUES (?,?,?,?,?,?,?)",
            ("trade-parent", 1, 1, 2410.0, 0.05, 888, "open"),
        )
    bridge = _FakeBridge(positions=[{"ticket": 888, "volume": 0.05}])
    with mock.patch.object(sync, "record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(sync.sync_closed_mt5_positions(bridge, {}))
    assert not rc.called
    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_simulated_trades WHERE trade_id=?", ("trade-parent",)
        ).fetchone()[0]
    assert status == "open"


def test_untracked_position_imported_when_other_trade_tracked(fresh_db):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(positions=[
        {"ticket": 555, "volume": 0.10},
        {"ticket": 777, "type": "BUY", "volume": 0.05, "open_price": 2400.0,
         "sl": 2390.0, "tp": 2410.0, "open_time": time.time()},
    ])
    asyncio.run(sync.sync_closed_mt5_positions(bridge, {}))
    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE mt5_ticket=?", (777,)).fetchone()
        )
    assert row is not None
    assert row["signal_id"] == "MT5_DIRECT"
    assert row["tg_source"] == "MT5_imported"
    assert row["lot_size"] == 0.05
    assert row["stop_loss"] == 2390.0
    assert row["tp1"] == 2410.0


def test_known_ladder_leg_ticket_not_reimported(fresh_db):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-parent", "BUY", 2400.0, 2400.0, 2390.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trade-parent", "sig-parent", 900, "BUY", 2400.0, 2400.0, 2400.0, 0.10, 0.10,
             2390.0, "closed", time.time(), "python"),
        )
        conn.execute(
            "INSERT INTO vantage_ladder_legs (trade_id, tier, tp_num, tp_price, lots, "
            "mt5_ticket, status) VALUES (?,?,?,?,?,?,?)",
            ("trade-parent", 1, 1, 2410.0, 0.05, 888, "open"),
        )
    _insert_trade("t-2", mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(positions=[
        {"ticket": 555, "volume": 0.10},
        {"ticket": 888, "type": "BUY", "volume": 0.05, "open_price": 2400.0},
    ])
    asyncio.run(sync.sync_closed_mt5_positions(bridge, {}))
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades WHERE mt5_ticket=?", (888,)).fetchone()[0]
    assert n == 0
