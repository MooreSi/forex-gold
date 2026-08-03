"""Characterizes _try_activate_pending_signals on SimulationEngine
(core/engine.py) before task 020 extracts it -- see
docs/todo/refactor/core-pending-signal-activation-migration/010-*.md.

open_trade_from_signal (already extracted, pack 13) is mocked in every
test here -- its own real behavior was already characterized in its own
extraction pack. NO real or demo MT5 order is ever placed, closed, or
modified.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.signals import pending_activation as psa
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
    pass


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._pending_activation_retry_after = {}
    e._dpm_candles = []
    e._bridge = _FakeBridge()
    e._cfg = {}
    e.background_open_commentary = None
    return e


_TICK = SimpleNamespace(bid=2400.0, ask=2400.5)
_TICK_OUTSIDE = SimpleNamespace(bid=2410.0, ask=2410.5)
_RS = {"max_open_trades": 1, "trade_strategy": "scale_out"}


def _insert_signal(sig_id="sig-1", direction="BUY", entry_low=2399.0, entry_high=2401.0,
                   sl=2390.0, tp1=2410.0, source="Chan", created_at=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, tp1, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sig_id, source, direction, entry_low, entry_high, sl, tp1, "pending",
             created_at if created_at is not None else time.time()),
        )


def _signal_status(sig_id="sig-1"):
    with db.db() as conn:
        return conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?", (sig_id,)).fetchone()[0]


def _run(engine, tick=_TICK, rs=None):
    with mock.patch.object(psa, "get_open_trades", return_value=[]):
        return asyncio.run(SimulationEngine._try_activate_pending_signals(engine, tick, rs or _RS))


