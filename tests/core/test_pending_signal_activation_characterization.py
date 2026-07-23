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

from forex_trader.core import database as db
from forex_trader.core import core_pending_signal_activation as psa
from forex_trader.core.engine import SimulationEngine


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
    e._background_open_commentary = None
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


def test_no_pending_signals_returns_false(fresh_db, engine):
    with mock.patch.object(psa, "get_open_trades", return_value=[]):
        result = asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert result is False


def test_expired_signal_marked_expired_no_activation(fresh_db, engine):
    _insert_signal(created_at=time.time() - 200)
    engine._pending_activation_retry_after["sig-1"] = 999999.0
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        result = asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert result is True
    assert _signal_status() == "expired"
    assert not ot.called
    assert "sig-1" not in engine._pending_activation_retry_after


def test_reversal_runner_gets_four_hour_expiry(fresh_db, engine):
    _insert_signal(created_at=time.time() - 200, tp1=2410.0)
    rs = {"max_open_trades": 1, "trade_strategy": "reversal_runner"}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, rs))
    assert _signal_status() != "expired"
    assert ot.called


def test_active_backoff_skips_all_checks(fresh_db, engine):
    _insert_signal()
    engine._pending_activation_retry_after["sig-1"] = time.time() + 100
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        result = asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert result is True
    assert not ot.called


def test_price_outside_zone_skips(fresh_db, engine):
    _insert_signal()
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        result = asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK_OUTSIDE, _RS))
    assert result is True
    assert not ot.called


def test_max_trades_reached_breaks_loop(fresh_db, engine):
    _insert_signal("sig-1", created_at=time.time())
    _insert_signal("sig-2", created_at=time.time() + 1)
    with mock.patch.object(psa, "get_open_trades", return_value=[{"trade_id": "x"}]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert not ot.called


def test_pre_trade_rr_filter_blocks_normal_strategy(fresh_db, engine):
    _insert_signal(tp1=2401.0)  # 1pt TP1 vs 10pt SL -> RR 0.1 < 0.75
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert not ot.called


def test_self_managed_strategy_bypasses_rr_filter(fresh_db, engine):
    _insert_signal(tp1=2401.0)
    rs = {"max_open_trades": 1, "trade_strategy": "conservative"}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, rs))
    assert ot.called


def test_duplicate_open_trade_marks_activated_skips(fresh_db, engine):
    _insert_signal()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, entry_low, "
            "entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("trade-existing", "sig-1", "BUY", 2399.0, 2401.0, 2400.0, 0.10, 0.10, 2390.0,
             "open", time.time()),
        )
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert not ot.called
    assert _signal_status() == "activated"


def test_momentum_mismatch_skips(fresh_db, engine):
    _insert_signal(tp1=2410.0)
    engine._dpm_candles = [{"open": 2405.0, "close": 2400.0}]  # bearish, signal is BUY
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert not ot.called


def test_momentum_match_proceeds(fresh_db, engine):
    _insert_signal(tp1=2410.0)
    engine._dpm_candles = [{"open": 2398.0, "close": 2401.0}]  # bullish, matches BUY
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert ot.called


def test_successful_activation_calls_with_full_lot_mult_flips_tg_status(fresh_db, engine):
    _insert_signal(tp1=2410.0)
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,group_name,sender_name,"
            "message_ts,raw_text,parsed_at,direction,status,signal_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("tg-1", "grp", "Chan", "sender", "", "text", time.time(), "BUY", "pending", "sig-1"),
        )
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert ot.call_args.args[0] is engine._bridge
    assert ot.call_args.args[1] == "sig-1"
    assert ot.call_args.kwargs["age_lot_mult"] == 1.0
    assert ot.call_args.kwargs["background_open_commentary"] is engine._background_open_commentary
    with db.db() as conn:
        tg_status = conn.execute("SELECT status FROM vantage_tg_signals WHERE tg_message_id=?", ("tg-1",)).fetchone()[0]
    assert tg_status == "activated"
    assert "sig-1" not in engine._pending_activation_retry_after


def test_activation_exception_sets_backoff(fresh_db, engine):
    _insert_signal(tp1=2410.0)
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(side_effect=RuntimeError("R:R filter blocked"))):
        asyncio.run(SimulationEngine._try_activate_pending_signals(engine, _TICK, _RS))
    assert "sig-1" in engine._pending_activation_retry_after
    assert engine._pending_activation_retry_after["sig-1"] > time.time()
