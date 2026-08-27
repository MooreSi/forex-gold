"""Characterizes _apply_followup_to_instant_trade/
_find_and_apply_instant_followup/_ime_timeout_watchdog on SimulationEngine
(core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-instant-followup-migration/010-*.md.

Uses a fake bridge (modify_order). NO real or demo MT5 order is ever
placed, closed, or modified -- verified via the fake's own call log.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend.src.runtime import SimulationEngine

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.utils.models import STRATEGY_BE_RUNNER
from tests._fakes import _FakeBridge


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge()
    return e


def _insert_trade(trade_id="trade-abc", signal_id="sig-x", strategy="scale_out",
                  direction="BUY", entry_price=2415.0, stop_loss=2403.0, mt5_ticket=777,
                  tg_source="Chan", managed_by="python"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (signal_id, direction, entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy, tg_source, managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, mt5_ticket, direction, entry_price, entry_price, entry_price,
             0.10, 0.10, stop_loss, "open", time.time(), strategy, tg_source, managed_by),
        )


def _insert_tg(tg_id="tg-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,group_name,sender_name,"
            "message_ts,raw_text,parsed_at,direction,status) VALUES (?,?,?,?,?,?,?,?,?)",
            (tg_id, "grp", "Chan", "sender", "", "text", time.time(), direction, "instant_activated"),
        )


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def _tg_row(tg_id):
    with db.db() as conn:
        row = conn.execute(
            "SELECT status, signal_id FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)
        ).fetchone()
        return tuple(row) if row else None


_PARSED_2TP = {"stop_loss": 2400.0, "entry_low": 2415.0, "entry_high": 2415.0,
              "tp1": 2420.0, "tp2": 2425.0}
_PARSED_1TP = {"stop_loss": 2400.0, "entry_low": 2415.0, "entry_high": 2415.0, "tp1": 2420.0}


# ── _apply_followup_to_instant_trade ─────────────────────────────────────────

def test_self_managed_no_mismatch_acknowledged_only(fresh_db, engine):
    _insert_trade(strategy="conservative")
    _insert_tg("tg-1")
    trade = _trade_dict("trade-abc")
    asyncio.run(TradingRuntime.apply_followup_to_instant_trade(
        engine, trade, _PARSED_2TP, "tg-1", "Chan", "Chan",
    ))
    trade_after = _trade_dict("trade-abc")
    assert trade_after["stop_loss"] == 2403.0  # unchanged
    assert _tg_row("tg-1") == ("followup_applied", "sig-x")


def test_self_managed_channel_override_mismatch_corrects_and_applies(fresh_db, engine):
    _insert_trade(strategy="conservative")
    _insert_tg("tg-2")
    db.set_channel_strategy_override("Chan", "reversal_runner")
    trade = _trade_dict("trade-abc")
    asyncio.run(TradingRuntime.apply_followup_to_instant_trade(
        engine, trade, _PARSED_2TP, "tg-2", "Chan", "Chan",
    ))
    trade_after = _trade_dict("trade-abc")
    assert trade_after["strategy"] == "reversal_runner"
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["tp1"] == 2420.0
    assert trade_after["tp2"] == 2425.0


def test_ea_managed_trade_acknowledged_only_not_overwritten(fresh_db, engine):
    """EA Templates (and any managed_by='ea' trade) compute their own SL/TP
    inside the EA's on-tick logic -- the follow-up signal's own price ladder
    must never overwrite that. Regression for trade 86576593 (Gold Diggers
    VIP / EA Template "Staged Ratchet 100-500"), where this function stamped
    the signal's raw TP ladder and a near-zero SL over the template's own
    100p SL / staged ratchet."""
    _insert_trade(strategy="template:Staged Ratchet 100-500", managed_by="ea")
    _insert_tg("tg-ea")
    trade = _trade_dict("trade-abc")
    asyncio.run(SimulationEngine.apply_followup_to_instant_trade(
        engine, trade, _PARSED_2TP, "tg-ea", "Chan", "Chan",
    ))
    trade_after = _trade_dict("trade-abc")
    assert trade_after["stop_loss"] == 2403.0  # unchanged -- template owns it
    assert trade_after["tp1"] is None
    assert trade_after["tp2"] is None
    assert engine._bridge.modify_order_calls == []
    assert _tg_row("tg-ea") == ("followup_applied", "sig-x")


def test_two_valid_tps_applied_as_parsed(fresh_db, engine):
    _insert_trade(strategy="scale_out")
    _insert_tg("tg-3")
    trade = _trade_dict("trade-abc")
    asyncio.run(TradingRuntime.apply_followup_to_instant_trade(
        engine, trade, _PARSED_2TP, "tg-3", "Chan", "Chan",
    ))
    trade_after = _trade_dict("trade-abc")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["tp1"] == 2420.0
    assert trade_after["tp2"] == 2425.0


def test_fewer_than_two_valid_tps_auto_spaces_from_fill(fresh_db, engine):
    _insert_trade(strategy="scale_out")
    _insert_tg("tg-4")
    trade = _trade_dict("trade-abc")
    asyncio.run(TradingRuntime.apply_followup_to_instant_trade(
        engine, trade, _PARSED_1TP, "tg-4", "Chan", "Chan",
    ))
    trade_after = _trade_dict("trade-abc")
    assert [trade_after[f"tp{i}"] for i in range(1, 7)] == [2418.0, 2420.0, 2422.0, 2425.0, 2429.0, 2433.0]
    assert trade_after["tp7"] is None
    assert trade_after["tp8"] is None


def test_no_signal_id_fallback_direct_db_update_and_modify_order(fresh_db, engine):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("", "BUY", 2415.0, 2415.0, 2403.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trade-so", "", 999, "BUY", 2415.0, 2415.0, 2415.0, 0.10, 0.10, 2403.0,
             "open", time.time(), "scale_out"),
        )
    _insert_tg("tg-6")
    trade = _trade_dict("trade-so")
    asyncio.run(TradingRuntime.apply_followup_to_instant_trade(
        engine, trade, {"stop_loss": 2400.0, "entry_low": 2415.0, "entry_high": 2415.0,
                        "tp1": 2418.0, "tp2": 2422.0, "tp3": 2430.0},
        "tg-6", "Chan", "Chan",
    ))
    assert engine._bridge.modify_order_calls == [{"ticket": 999, "sl": 2400.0, "tp": None}]


def test_no_signal_id_be_runner_uses_highest_profitable_tp(fresh_db, engine):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("", "BUY", 2415.0, 2415.0, 2403.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trade-be", "", 888, "BUY", 2415.0, 2415.0, 2415.0, 0.10, 0.10, 2403.0,
             "open", time.time(), STRATEGY_BE_RUNNER),
        )
    _insert_tg("tg-5")
    trade = _trade_dict("trade-be")
    asyncio.run(TradingRuntime.apply_followup_to_instant_trade(
        engine, trade, {"stop_loss": 2400.0, "entry_low": 2415.0, "entry_high": 2415.0,
                        "tp1": 2418.0, "tp2": 2422.0, "tp3": 2430.0},
        "tg-5", "Chan", "Chan",
    ))
    assert engine._bridge.modify_order_calls == [{"ticket": 888, "sl": 2400.0, "tp": 2430.0}]


# ── _find_and_apply_instant_followup ────────────────────────────────────────

def test_find_followup_no_match_returns_false(fresh_db, engine):
    result = asyncio.run(TradingRuntime._find_and_apply_instant_followup(
        engine, "Chan", "BUY", _PARSED_2TP, "tg-1",
    ))
    assert result is False


def test_find_followup_direction_mismatch_returns_false(fresh_db, engine):
    _insert_trade(direction="SELL", stop_loss=2420.0, tg_source="Chan")
    result = asyncio.run(TradingRuntime._find_and_apply_instant_followup(
        engine, "Chan", "BUY", _PARSED_2TP, "tg-2",
    ))
    assert result is False


def test_find_followup_match_found_applies_and_returns_true(fresh_db, engine):
    _insert_trade(tg_source="Chan")
    _insert_tg("tg-3")
    result = asyncio.run(TradingRuntime._find_and_apply_instant_followup(
        engine, "Chan", "BUY", _PARSED_2TP, "tg-3",
    ))
    assert result is True
    assert _trade_dict("trade-abc")["stop_loss"] == 2400.0


# ── _ime_timeout_watchdog ────────────────────────────────────────────────────

def _insert_stale(trade_id, strategy="scale_out", mt5_ticket=777,
                  entry_price=2415.0, stop_loss=2403.0, age_s=200):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, "BUY", entry_price, entry_price,
             entry_price, 0.10, 0.10, stop_loss, "open", time.time() - age_s, strategy),
        )


