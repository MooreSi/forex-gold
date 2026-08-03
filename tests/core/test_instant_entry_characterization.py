"""Characterizes _process_instant_entry on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-instant-entry-migration/010-*.md.

open_trade (already extracted, pack 11) is mocked in every test here --
its own real behavior was already characterized in its own extraction
pack. NO real or demo MT5 order is ever placed, closed, or modified --
open_trade is never given a real bridge to act through.
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.services.trading import instant_entry as core_instant_entry
from backend.src.db import database as db
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
    def __init__(self):
        self.modify_order_calls = []
        self.get_tick = mock.AsyncMock(return_value=None)

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._dpm_candles = []
    e._cfg = {}
    return e


# Evaluated per call, not at import. As a module-level constant this was
# fixed at collection time, so by the time these tests ran near the end of
# a ~6 minute suite the "fresh" timestamp was older than the production
# staleness threshold (_MAX_SIGNAL_AGE_SECS = 4 minutes) and the signal was
# correctly rejected as stale. Passed in isolation, failed in the full run.
def _fresh_ts() -> str:
    return datetime.now(timezone.utc).isoformat()
_STALE_TS = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
_TICK = SimpleNamespace(bid=2414.5, ask=2415.0, spread_points=10.0)
_TRADE_RESULT = {"entry_price": 2415.0, "mt5_ticket": 777, "trade_id": "trade-abc", "managed_by": "app"}


def _rs():
    return db.get_risk_settings()


def _signals_count():
    with db.db() as conn:
        return conn.execute("SELECT COUNT(*) FROM vantage_signals").fetchone()[0]


def _tg_status(tg_id):
    with db.db() as conn:
        row = conn.execute(
            "SELECT status FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)
        ).fetchone()
        return row[0] if row else None


def _run(engine, msg, tg_id, direction, price, rs, auto_execute, text="XAU Buy Now"):
    return asyncio.run(SimulationEngine._process_instant_entry(
        engine, msg, tg_id, "grp-1", "Chan", text, direction, price, rs, auto_execute,
    ))


def _run_open_trade_path(engine, rs, tg_id="tg-8"):
    engine._bridge.get_tick = mock.AsyncMock(return_value=_TICK)
    with mock.patch.object(core_instant_entry, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(core_instant_entry, "get_open_trades", return_value=[]), \
         mock.patch.object(core_instant_entry, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)) as ot:
        _run(engine, {"timestamp": _fresh_ts()}, tg_id, "BUY", None, rs, True)
    return ot


def _insert_open_trade_for_postfill(trade_id="trade-abc"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-x", "BUY", 2415.0, 2415.0, 2403.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "sig-x", 777, "BUY", 2415.0, 2415.0, 2415.0, 0.01, 0.01, 2403.0,
             "open", time.time()),
        )


