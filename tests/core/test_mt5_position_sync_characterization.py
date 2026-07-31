"""Characterizes _sync_closed_mt5_positions on SimulationEngine
(core/engine.py) before task 020 extracts it -- see
docs/todo/refactor/core-mt5-position-sync-migration/010-*.md.

Uses a fake bridge. telegram_alerts.fmt_trade_close is mocked in full-close
tests (called eagerly before asyncio.create_task, so mocking send_message
alone doesn't isolate it). NO real or demo MT5 order is ever placed,
closed, or modified -- this function never calls a bridge order-mutation
method at all.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import alerts as telegram_alerts
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


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._mt5_sync_missing_streak = {}
    return e


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


def test_bridge_not_configured_returns_immediately(fresh_db, engine):
    engine._bridge = _FakeBridge(configured=False)
    asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))  # must not raise


def test_no_open_trades_skips_import_pass_too(fresh_db, engine):
    engine._bridge = _FakeBridge(positions=[
        {"ticket": 777, "type": "BUY", "volume": 0.05, "open_price": 2400.0},
    ])
    asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
    assert n == 0  # untracked position never imported -- no open_trades to reach that pass


def test_ticket_still_live_clears_streak(fresh_db, engine):
    _insert_trade(mt5_ticket=555)
    engine._bridge = _FakeBridge(positions=[{"ticket": 555, "volume": 0.10}])
    engine._mt5_sync_missing_streak = {"t-1": 1}
    asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    assert "t-1" not in engine._mt5_sync_missing_streak


def test_disconnected_bridge_empty_positions_skips_whole_sync(fresh_db, engine):
    _insert_trade(mt5_ticket=555)
    engine._bridge = _FakeBridge(positions=[], health={"connected": False})
    with mock.patch.object(SimulationEngine, "_record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    assert not rc.called


def test_miss_streak_below_threshold_not_yet_closed(fresh_db, engine):
    _insert_trade(mt5_ticket=555)
    engine._bridge = _FakeBridge(positions=[])
    with mock.patch.object(SimulationEngine, "_record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    assert not rc.called
    assert engine._mt5_sync_missing_streak["t-1"] == 1


def test_miss_streak_reaches_threshold_full_close_fallback_price(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    engine._bridge = _FakeBridge(positions=[], deal_history=[], position_history=[])
    engine._mt5_sync_missing_streak = {"t-1": 1}
    with mock.patch.object(SimulationEngine, "_record_close", new=mock.AsyncMock(return_value={"net_pnl": 0})) as rc, \
         mock.patch.object(SimulationEngine, "_sync_profit", new=mock.AsyncMock()), \
         mock.patch.object(SimulationEngine, "_schedule_profit_sync", new=mock.AsyncMock()), \
         mock.patch.object(telegram_alerts, "fmt_trade_close", return_value="msg"), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    rc.assert_called_once_with("t-1", 2400.0, "MT5_close")


def test_full_close_infers_sl_reason_from_comment(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    deals = [{"entry": 1, "position_id": 555, "price": 2390.0, "time": 100,
             "comment": "sl 2390", "volume": 0.10, "profit": -10.0}]
    engine._bridge = _FakeBridge(positions=[], deal_history=[], position_history=deals)
    engine._mt5_sync_missing_streak = {"t-1": 1}
    with mock.patch.object(SimulationEngine, "_record_close", new=mock.AsyncMock(return_value={"net_pnl": -10})) as rc, \
         mock.patch.object(SimulationEngine, "_sync_profit", new=mock.AsyncMock()), \
         mock.patch.object(SimulationEngine, "_schedule_profit_sync", new=mock.AsyncMock()), \
         mock.patch.object(telegram_alerts, "fmt_trade_close", return_value="msg"), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    rc.assert_called_once_with("t-1", 2390.0, "SL")


def test_partial_close_detected_no_full_close_double_prefixed_reason(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    partial_deals = [{"entry": 1, "position_id": 555, "price": 2405.0, "time": 100,
                      "comment": "tp", "volume": 0.05, "profit": 5.0, "swap": 0, "fee": 0}]
    engine._bridge = _FakeBridge(positions=[{"ticket": 999, "volume": 0.05}],
                                 deal_history=[], position_history=partial_deals)
    engine._mt5_sync_missing_streak = {"t-1": 1}
    with mock.patch.object(SimulationEngine, "partial_close_trade",
                           new=mock.AsyncMock(return_value={"partial_pnl": 5.0})) as pc, \
         mock.patch.object(SimulationEngine, "_record_close", new=mock.AsyncMock()) as rc, \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    pc.assert_called_once_with("t-1", 0.05, 2405.0, "MT5_MT5_sync_TP")
    assert not rc.called
    assert "t-1" not in engine._mt5_sync_missing_streak


def test_partial_close_reassigns_ticket_when_continuing_position_found(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    partial_deals = [{"entry": 1, "position_id": 555, "price": 2405.0, "time": 100,
                      "comment": "tp", "volume": 0.05, "profit": 5.0, "swap": 0, "fee": 0}]
    engine._bridge = _FakeBridge(positions=[{"ticket": 999, "volume": 0.05}],
                                 deal_history=[], position_history=partial_deals)
    engine._mt5_sync_missing_streak = {"t-1": 1}
    with mock.patch.object(SimulationEngine, "partial_close_trade",
                           new=mock.AsyncMock(return_value={"partial_pnl": 5.0})), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    assert _trade_dict()["mt5_ticket"] == 999


def test_ladder_leg_trade_excluded_from_close_detection(fresh_db, engine):
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
    engine._bridge = _FakeBridge(positions=[{"ticket": 888, "volume": 0.05}])
    with mock.patch.object(SimulationEngine, "_record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    assert not rc.called
    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_simulated_trades WHERE trade_id=?", ("trade-parent",)
        ).fetchone()[0]
    assert status == "open"


def test_untracked_position_imported_when_other_trade_tracked(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.10)
    engine._bridge = _FakeBridge(positions=[
        {"ticket": 555, "volume": 0.10},
        {"ticket": 777, "type": "BUY", "volume": 0.05, "open_price": 2400.0,
         "sl": 2390.0, "tp": 2410.0, "open_time": time.time()},
    ])
    asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
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


def test_known_ladder_leg_ticket_not_reimported(fresh_db, engine):
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
    engine._bridge = _FakeBridge(positions=[
        {"ticket": 555, "volume": 0.10},
        {"ticket": 888, "type": "BUY", "volume": 0.05, "open_price": 2400.0},
    ])
    asyncio.run(SimulationEngine._sync_closed_mt5_positions(engine))
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades WHERE mt5_ticket=?", (888,)).fetchone()[0]
    assert n == 0
