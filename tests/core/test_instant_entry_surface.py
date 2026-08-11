"""Proves backend.src.services.trading.instant_entry's extracted function
behaves identically to SimulationEngine._process_instant_entry,
characterized in test_instant_entry_characterization.py -- see
docs/todo/refactor/core-instant-entry-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. open_trade is mocked in every test here -- its own real behavior
was already characterized in its own extraction pack. NO real or demo
MT5 order is ever placed, closed, or modified.
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.trading import instant_entry as ime


class _FakeBridge:
    def __init__(self, tick=None):
        self.modify_order_calls = []
        self._tick = tick if tick is not None else _TICK

    async def get_tick(self):
        return self._tick

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


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


def _run(bridge, msg, tg_id, direction, price, rs, auto_execute, text="XAU Buy Now"):
    return asyncio.run(ime.process_instant_entry(
        msg, tg_id, "grp-1", "Chan", text, direction, price, rs, auto_execute,
        bridge, [],
    ))


def test_stale_message_recorded_historical_no_signal(fresh_db):
    _run(_FakeBridge(), {"timestamp": _STALE_TS}, "tg-1", "BUY", None, _rs(), True)
    assert _tg_status("tg-1") == "instant_historical"
    assert _signals_count() == 0


def test_no_timestamp_treated_as_stale(fresh_db):
    _run(_FakeBridge(), {}, "tg-2", "BUY", None, _rs(), True)
    assert _tg_status("tg-2") == "instant_historical"


def test_auto_execute_off_recorded_pending_no_signal(fresh_db):
    _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-3", "BUY", None, _rs(), False)
    assert _tg_status("tg-3") == "instant_pending"
    assert _signals_count() == 0


def test_session_blocked_no_signal(fresh_db):
    with mock.patch.object(db, "is_session_allowed", return_value=(False, "Asian")):
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-4", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def test_trading_schedule_blocked_no_signal(fresh_db):
    """IME previously never checked the Trading Schedule at all (only
    resolve_open_trade_params()'s automated-signal path did), so a hit
    profit target did not stop new instant-entry trades -- confirmed live
    2026-07-23."""
    with mock.patch.object(
        ime, "check_trading_schedule",
        return_value=(False, "profit target reached for this window ($50.00 of $50.00)"),
    ):
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-4b", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def test_no_tick_no_signal(fresh_db):
    _run(_FakeBridge(tick=None), {"timestamp": _fresh_ts()}, "tg-5", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def test_wide_spread_no_signal(fresh_db):
    wide_tick = SimpleNamespace(bid=2414.5, ask=2415.0, spread_points=100.0)
    _run(_FakeBridge(tick=wide_tick), {"timestamp": _fresh_ts()}, "tg-6", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def test_max_open_trades_reached_no_signal(fresh_db):
    with mock.patch.object(ime, "get_open_trades", return_value=[{"trade_id": "x"}]):
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-7", "BUY", None, _rs(), True)
    assert _signals_count() == 0


def _run_open_trade_path(rs, tg_id="tg-8"):
    with mock.patch.object(ime, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(ime, "get_open_trades", return_value=[]), \
         mock.patch.object(ime, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)) as ot:
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, tg_id, "BUY", None, rs, True)
    return ot


def test_default_risk_pct_sizing_floors_to_min_lot(fresh_db):
    ot = _run_open_trade_path(_rs())
    assert ot.called
    assert ot.call_args.kwargs["lot_size"] == 0.01
    assert ot.call_args.kwargs["stop_loss"] == 2403.0


def test_governor_on_default_settings_skips_entirely(fresh_db):
    rs = dict(_rs(), risk_governor_enabled=1, strategy_lot_size=0.0)
    with mock.patch.object(ime, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(ime, "get_open_trades", return_value=[]), \
         mock.patch.object(ime, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)) as ot:
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-9", "BUY", None, rs, True)
    assert not ot.called


def test_governor_on_max_risk_cap_binds_over_raw_risk_pct(fresh_db):
    rs = dict(_rs(), risk_governor_enabled=1, strategy_lot_size=0.0,
              risk_per_trade_pct=5.0, max_risk_per_trade_pct=1.0)
    ot = _run_open_trade_path(rs, tg_id="tg-10")
    assert ot.called
    assert ot.call_args.kwargs["lot_size"] == 0.01
    assert ot.call_args.kwargs["stop_loss"] == 2403.0


def test_governor_on_fixed_lot_uses_it_directly(fresh_db):
    rs = dict(_rs(), risk_governor_enabled=1, strategy_lot_size=0.05)
    ot = _run_open_trade_path(rs, tg_id="tg-11")
    assert ot.call_args.kwargs["lot_size"] == 0.05
    assert ot.call_args.kwargs["stop_loss"] == 2403.0


def test_governor_off_fixed_lot_uses_150_cap_distance(fresh_db):
    rs = dict(_rs(), risk_governor_enabled=0, strategy_lot_size=0.05)
    ot = _run_open_trade_path(rs, tg_id="tg-12")
    assert ot.call_args.kwargs["lot_size"] == 0.05
    assert ot.call_args.kwargs["stop_loss"] == 2390.0


def test_channel_override_auto_uses_last_claude_rec(fresh_db):
    db.set_channel_strategy_override("Chan", "auto")
    db.set_channel_strategy_rec("Chan", "reversal_runner", "trending", 0.8)
    ot = _run_open_trade_path(_rs(), tg_id="tg-13")
    assert ot.call_args.kwargs["strategy"] == "reversal_runner"


def test_channel_override_explicit_used_directly(fresh_db):
    db.set_channel_strategy_override("Chan", "trail_stop")
    ot = _run_open_trade_path(_rs(), tg_id="tg-14")
    assert ot.call_args.kwargs["strategy"] == "trail_stop"


def test_high_risk_text_forces_conservative(fresh_db):
    with mock.patch.object(ime, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(ime, "get_open_trades", return_value=[]), \
         mock.patch.object(ime, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)) as ot:
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-15", "BUY", None, _rs(), True,
             text="XAU Buy Now — High Risk")
    assert ot.call_args.kwargs["strategy"] == "conservative"


def _insert_open_trade_for_postfill(trade_id="trade-abc", lot_size=0.01):
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
            (trade_id, "sig-x", 777, "BUY", 2415.0, 2415.0, 2415.0, lot_size, lot_size, 2403.0,
             "open", time.time()),
        )


def test_conservative_post_fill_overrides_sl_tp1_from_fill(fresh_db):
    _insert_open_trade_for_postfill()
    rs = dict(_rs(), trade_strategy="conservative")
    bridge = _FakeBridge()
    with mock.patch.object(ime, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(ime, "get_open_trades", return_value=[]), \
         mock.patch.object(ime, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)):
        _run(bridge, {"timestamp": _fresh_ts()}, "tg-16", "BUY", None, rs, True)

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", ("trade-abc",)).fetchone()
        )
    assert row["stop_loss"] == 2410.0
    assert row["tp1"] == 2418.0
    assert row["tp2"] is None
    assert bridge.modify_order_calls == [{"ticket": 777, "sl": 2410.0, "tp": None}]


def test_conservative_trial_post_fill_sets_six_tp_ladder(fresh_db):
    _insert_open_trade_for_postfill(lot_size=0.10)
    rs = dict(_rs(), trade_strategy="conservative_trial", strategy_lot_size=0.10)
    bridge = _FakeBridge()
    with mock.patch.object(ime, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(ime, "get_open_trades", return_value=[]), \
         mock.patch.object(ime, "open_trade", new=mock.AsyncMock(return_value=_TRADE_RESULT)):
        _run(bridge, {"timestamp": _fresh_ts()}, "tg-17", "BUY", None, rs, True)

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", ("trade-abc",)).fetchone()
        )
    assert row["stop_loss"] == 2405.0
    assert [row[f"tp{i}"] for i in range(1, 7)] == [2420.0, 2425.0, 2429.0, 2435.0, 2442.0, 2450.0]


def test_open_trade_exception_rolls_back_signal_and_marks_failed(fresh_db):
    with mock.patch.object(ime, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(ime, "get_open_trades", return_value=[]), \
         mock.patch.object(ime, "open_trade", new=mock.AsyncMock(side_effect=RuntimeError("mt5 fail"))):
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-18", "BUY", None, _rs(), True)

    assert _signals_count() == 0
    assert _tg_status("tg-18") == "instant_failed"


def test_circuit_breaker_exception_same_cleanup_as_generic(fresh_db):
    with mock.patch.object(ime, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(ime, "get_open_trades", return_value=[]), \
         mock.patch.object(ime, "open_trade",
                           new=mock.AsyncMock(side_effect=ValueError("circuit breaker tripped"))):
        _run(_FakeBridge(), {"timestamp": _fresh_ts()}, "tg-19", "BUY", None, _rs(), True)

    assert _signals_count() == 0
    assert _tg_status("tg-19") == "instant_failed"
