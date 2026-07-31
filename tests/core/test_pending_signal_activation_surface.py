"""Proves backend.src.services.signals.pending_activation's extracted
function behaves identically to SimulationEngine._try_activate_pending_signals,
characterized in test_pending_signal_activation_characterization.py -- see
docs/todo/refactor/core-pending-signal-activation-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified --
open_trade_from_signal is mocked in every test here.

Note: the extracted function takes `dpm_candles`/`starting_balance` as
explicit parameters and forwards them to open_trade_from_signal (also
extracted, pack 13) -- the original bound method read them implicitly via
`self._dpm_candles`/`self._cfg`, so its own call to
`self.open_trade_from_signal(...)` only ever passed `age_lot_mult`
explicitly. The call-shape assertion below reflects the new, wider
explicit signature; the underlying behavior (what state ends up where) is
unchanged.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from backend.src.services.signals import pending_activation as psa


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


def _run(tick, rs, bridge, retry_after, dpm_candles):
    with mock.patch.object(psa, "get_open_trades", return_value=[]):
        return asyncio.run(psa.try_activate_pending_signals(tick, rs, bridge, retry_after, dpm_candles))


def test_no_pending_signals_returns_false(fresh_db):
    result = _run(_TICK, _RS, _FakeBridge(), {}, [])
    assert result is False


def test_expired_signal_marked_expired_no_activation(fresh_db):
    _insert_signal(created_at=time.time() - 200)
    retry_after = {"sig-1": 999999.0}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        result = asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), retry_after, []))
    assert result is True
    assert _signal_status() == "expired"
    assert not ot.called
    assert "sig-1" not in retry_after


def test_reversal_runner_gets_four_hour_expiry(fresh_db):
    _insert_signal(created_at=time.time() - 200, tp1=2410.0)
    rs = {"max_open_trades": 1, "trade_strategy": "reversal_runner"}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, rs, _FakeBridge(), {}, []))
    assert _signal_status() != "expired"
    assert ot.called


def test_active_backoff_skips_all_checks(fresh_db):
    _insert_signal()
    retry_after = {"sig-1": time.time() + 100}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        result = asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), retry_after, []))
    assert result is True
    assert not ot.called


def test_price_outside_zone_skips(fresh_db):
    _insert_signal()
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        result = asyncio.run(psa.try_activate_pending_signals(_TICK_OUTSIDE, _RS, _FakeBridge(), {}, []))
    assert result is True
    assert not ot.called


def test_working_ea_pending_order_excludes_signal_from_market_fill(fresh_db):
    """A signal_id with a resting genuine EA pending order (Limit Runner /
    ORB auto-execute) must be excluded from this generic market-fill
    watcher entirely -- otherwise the moment price re-enters the same
    entry zone the EA order is also watching, this would open a SECOND,
    duplicate trade before the real pending order has even filled."""
    _insert_signal()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_pending_orders (trade_id,signal_id,channel_name,direction,"
            "price,stop_loss,tps_json,pcts_json,be_at_pos,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t1", "sig-1", "chan", "BUY", 2401.0, 2390.0, "{}", "[]", 0, 0.10,
             "working", time.time()),
        )
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        result = asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), {}, []))
    assert result is False  # no eligible pending signals at all
    assert not ot.called
    assert _signal_status() == "pending"  # untouched -- not expired, not activated


def test_resolved_ea_pending_order_no_longer_excludes_signal(fresh_db):
    """Once the pending order resolves (filled/cancelled), status moves off
    'working' -- the exclusion must not linger and permanently hide the
    signal from other callers that might still reference it by signal_id."""
    _insert_signal()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_pending_orders (trade_id,signal_id,channel_name,direction,"
            "price,stop_loss,tps_json,pcts_json,be_at_pos,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t1", "sig-1", "chan", "BUY", 2401.0, 2390.0, "{}", "[]", 0, 0.10,
             "cancelled", time.time()),
        )
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), {}, []))
    assert ot.called


def test_max_trades_reached_breaks_loop(fresh_db):
    _insert_signal("sig-1", created_at=time.time())
    _insert_signal("sig-2", created_at=time.time() + 1)
    with mock.patch.object(psa, "get_open_trades", return_value=[{"trade_id": "x"}]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), {}, []))
    assert not ot.called


def test_pre_trade_rr_filter_blocks_normal_strategy(fresh_db):
    _insert_signal(tp1=2401.0)
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), {}, []))
    assert not ot.called


def test_self_managed_strategy_bypasses_rr_filter(fresh_db):
    _insert_signal(tp1=2401.0)
    rs = {"max_open_trades": 1, "trade_strategy": "conservative"}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, rs, _FakeBridge(), {}, []))
    assert ot.called


def test_duplicate_open_trade_marks_activated_skips(fresh_db):
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
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), {}, []))
    assert not ot.called
    assert _signal_status() == "activated"


def test_momentum_mismatch_skips(fresh_db):
    _insert_signal(tp1=2410.0)
    dpm_candles = [{"open": 2405.0, "close": 2400.0}]
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), {}, dpm_candles))
    assert not ot.called


def test_momentum_match_proceeds(fresh_db):
    _insert_signal(tp1=2410.0)
    dpm_candles = [{"open": 2398.0, "close": 2401.0}]
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), {}, dpm_candles))
    assert ot.called


def test_successful_activation_calls_with_full_lot_mult_flips_tg_status(fresh_db):
    _insert_signal(tp1=2410.0)
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,group_name,sender_name,"
            "message_ts,raw_text,parsed_at,direction,status,signal_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("tg-1", "grp", "Chan", "sender", "", "text", time.time(), "BUY", "pending", "sig-1"),
        )
    retry_after = {}
    bridge = _FakeBridge()
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, bridge, retry_after, []))
    assert ot.call_args.args[0] is bridge
    assert ot.call_args.args[1] == "sig-1"
    assert ot.call_args.kwargs["age_lot_mult"] == 1.0
    with db.db() as conn:
        tg_status = conn.execute("SELECT status FROM vantage_tg_signals WHERE tg_message_id=?", ("tg-1",)).fetchone()[0]
    assert tg_status == "activated"
    assert "sig-1" not in retry_after


def test_activation_exception_sets_backoff(fresh_db):
    _insert_signal(tp1=2410.0)
    retry_after = {}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(side_effect=RuntimeError("R:R filter blocked"))):
        asyncio.run(psa.try_activate_pending_signals(_TICK, _RS, _FakeBridge(), retry_after, []))
    assert "sig-1" in retry_after
    assert retry_after["sig-1"] > time.time()
