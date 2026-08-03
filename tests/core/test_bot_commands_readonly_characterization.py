"""Characterizes the read-only/toggle Telegram bot commands on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-bot-commands-readonly-migration/010-*.md.

No real or demo MT5 order is ever placed, closed, or modified -- this
cluster has no order-placing surface at all. Responses are checked via
substring assertions on the computed values, not full exact-string
matching.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime


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
    def __init__(self, account=None, tick=None):
        self._account = account or {}
        self._tick = tick

    async def get_account(self):
        return self._account

    async def get_tick(self):
        return self._tick


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge()
    e._tg_reader = None
    return e


def _insert_signal_and_trade(trade_id, direction="BUY", entry_price=2400.0,
                             remaining_lots=0.10, stop_loss=2390.0, tp1=None,
                             mt5_ticket=None, status="open", close_time=None,
                             close_price=None, mt5_profit=None, exit_reason=None,
                             open_time=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, tp1, status, "
            "open_time, close_time, close_price, mt5_profit, exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, direction, entry_price, entry_price,
             entry_price, remaining_lots, remaining_lots, stop_loss, tp1, status,
             open_time or time.time(), close_time, close_price, mt5_profit, exit_reason),
        )


# ── _cmd_help ──────────────────────────────────────────────────────────────

# ── _cmd_balance ──────────────────────────────────────────────────────────

# ── _cmd_daily ────────────────────────────────────────────────────────────

# ── _cmd_status ───────────────────────────────────────────────────────────

# ── _cmd_trades ───────────────────────────────────────────────────────────

# ── _cmd_pause / _cmd_resume ─────────────────────────────────────────────

# ── _cmd_risk ─────────────────────────────────────────────────────────────

# ── _cmd_strategy ─────────────────────────────────────────────────────────

# ── DPM/IME toggles ──────────────────────────────────────────────────────

