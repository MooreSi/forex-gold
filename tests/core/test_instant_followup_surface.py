"""Proves forex_trader.core.core_instant_followup's extracted functions
behave identically to SimulationEngine's originals, characterized in
test_instant_followup_characterization.py -- see
docs/todo/refactor/core-instant-followup-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_instant_followup as followup
from backend.src.utils.models import STRATEGY_BE_RUNNER


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

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


def _insert_trade(trade_id="trade-abc", signal_id="sig-x", strategy="scale_out",
                  direction="BUY", entry_price=2415.0, stop_loss=2403.0, mt5_ticket=777,
                  tg_source="Chan"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (signal_id, direction, entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, mt5_ticket, direction, entry_price, entry_price, entry_price,
             0.10, 0.10, stop_loss, "open", time.time(), strategy, tg_source),
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


# ── apply_followup_to_instant_trade ────────────────────────────────────────

def test_self_managed_no_mismatch_acknowledged_only(fresh_db):
    _insert_trade(strategy="conservative")
    _insert_tg("tg-1")
    bridge = _FakeBridge()
    trade = _trade_dict("trade-abc")
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, _PARSED_2TP, "tg-1", "Chan", "Chan", bridge,
    ))
    trade_after = _trade_dict("trade-abc")
    assert trade_after["stop_loss"] == 2403.0
    assert _tg_row("tg-1") == ("followup_applied", "sig-x")


def test_self_managed_channel_override_mismatch_corrects_and_applies(fresh_db):
    _insert_trade(strategy="conservative")
    _insert_tg("tg-2")
    db.set_channel_strategy_override("Chan", "reversal_runner")
    bridge = _FakeBridge()
    trade = _trade_dict("trade-abc")
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, _PARSED_2TP, "tg-2", "Chan", "Chan", bridge,
    ))
    trade_after = _trade_dict("trade-abc")
    assert trade_after["strategy"] == "reversal_runner"
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["tp1"] == 2420.0
    assert trade_after["tp2"] == 2425.0


def test_two_valid_tps_applied_as_parsed(fresh_db):
    _insert_trade(strategy="scale_out")
    _insert_tg("tg-3")
    bridge = _FakeBridge()
    trade = _trade_dict("trade-abc")
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, _PARSED_2TP, "tg-3", "Chan", "Chan", bridge,
    ))
    trade_after = _trade_dict("trade-abc")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["tp1"] == 2420.0
    assert trade_after["tp2"] == 2425.0


def test_fewer_than_two_valid_tps_auto_spaces_from_fill(fresh_db):
    _insert_trade(strategy="scale_out")
    _insert_tg("tg-4")
    bridge = _FakeBridge()
    trade = _trade_dict("trade-abc")
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, _PARSED_1TP, "tg-4", "Chan", "Chan", bridge,
    ))
    trade_after = _trade_dict("trade-abc")
    assert [trade_after[f"tp{i}"] for i in range(1, 7)] == [2418.0, 2420.0, 2422.0, 2425.0, 2429.0, 2433.0]
    assert trade_after["tp7"] is None
    assert trade_after["tp8"] is None


def test_sl_within_tolerance_of_signal_zone_applied_as_parsed(fresh_db):
    # entry_price=2415.0, signal zone mid=2415.0, SL 2403.0 -> 12pt from zone
    # AND from actual entry (same point here) -- well within the 1.5x
    # tolerance, so no adjustment should fire.
    _insert_trade(strategy="scale_out", entry_price=2415.0)
    _insert_tg("tg-sl1")
    bridge = _FakeBridge()
    trade = _trade_dict("trade-abc")
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, _PARSED_2TP, "tg-sl1", "Chan", "Chan", bridge,
    ))
    assert _trade_dict("trade-abc")["stop_loss"] == 2400.0


def test_sl_far_from_actual_entry_recomputed_from_zone_distance(fresh_db):
    # Regression test for ticket 1641075009: IME filled well outside the
    # signal's own zone -- signal zone 4098-4102 (mid 4100), SL 4096.0
    # (4pt from zone mid), but actual entry was 4116.24. Applying 4096.0
    # verbatim would be a 20.24pt stop (5x the intended distance) -- the
    # fix re-derives the SAME ~4pt distance from the actual fill instead.
    _insert_trade(strategy="adaptive_runner", entry_price=4116.24, stop_loss=4101.04)
    _insert_tg("tg-sl2")
    bridge = _FakeBridge()
    trade = _trade_dict("trade-abc")
    parsed = {
        "stop_loss": 4096.0, "entry_low": 4098.0, "entry_high": 4102.0,
        "tp1": 4119.24, "tp2": 4121.24,
    }
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, parsed, "tg-sl2", "Chan", "Chan", bridge,
    ))
    trade_after = _trade_dict("trade-abc")
    # 4116.24 - 4.0 = 4112.24, not the raw 4096.0
    assert trade_after["stop_loss"] == 4112.24


def test_sl_close_to_tolerance_boundary_not_adjusted(fresh_db):
    # intended_dist=4pt (zone mid 2415.0 to SL 2411.0), actual_dist from
    # entry 2420.0 is exactly 9pt = 2.25x -- above 1.5x, so this SHOULD
    # adjust; confirms the threshold is evaluated on distance, not just
    # presence of a zone/entry mismatch.
    _insert_trade(strategy="scale_out", entry_price=2420.0, stop_loss=2410.0)
    _insert_tg("tg-sl3")
    bridge = _FakeBridge()
    trade = _trade_dict("trade-abc")
    parsed = {
        "stop_loss": 2411.0, "entry_low": 2413.0, "entry_high": 2417.0,
        "tp1": 2430.0, "tp2": 2435.0,
    }
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, parsed, "tg-sl3", "Chan", "Chan", bridge,
    ))
    trade_after = _trade_dict("trade-abc")
    # 2420.0 - 4.0 = 2416.0, not the raw 2411.0
    assert trade_after["stop_loss"] == 2416.0


def test_no_signal_id_fallback_direct_db_update_and_modify_order(fresh_db):
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
    bridge = _FakeBridge()
    trade = _trade_dict("trade-so")
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, {"stop_loss": 2400.0, "entry_low": 2415.0, "entry_high": 2415.0,
               "tp1": 2418.0, "tp2": 2422.0, "tp3": 2430.0},
        "tg-6", "Chan", "Chan", bridge,
    ))
    assert bridge.modify_order_calls == [{"ticket": 999, "sl": 2400.0, "tp": None}]


def test_no_signal_id_be_runner_uses_highest_profitable_tp(fresh_db):
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
    bridge = _FakeBridge()
    trade = _trade_dict("trade-be")
    asyncio.run(followup.apply_followup_to_instant_trade(
        trade, {"stop_loss": 2400.0, "entry_low": 2415.0, "entry_high": 2415.0,
               "tp1": 2418.0, "tp2": 2422.0, "tp3": 2430.0},
        "tg-5", "Chan", "Chan", bridge,
    ))
    assert bridge.modify_order_calls == [{"ticket": 888, "sl": 2400.0, "tp": 2430.0}]


# ── find_and_apply_instant_followup ─────────────────────────────────────────

def test_find_followup_no_match_returns_false(fresh_db):
    bridge = _FakeBridge()
    result = asyncio.run(followup.find_and_apply_instant_followup(
        "Chan", "BUY", _PARSED_2TP, "tg-1", bridge,
    ))
    assert result is False


def test_find_followup_direction_mismatch_returns_false(fresh_db):
    _insert_trade(direction="SELL", stop_loss=2420.0, tg_source="Chan")
    bridge = _FakeBridge()
    result = asyncio.run(followup.find_and_apply_instant_followup(
        "Chan", "BUY", _PARSED_2TP, "tg-2", bridge,
    ))
    assert result is False


def test_find_followup_match_found_applies_and_returns_true(fresh_db):
    _insert_trade(tg_source="Chan")
    _insert_tg("tg-3")
    bridge = _FakeBridge()
    result = asyncio.run(followup.find_and_apply_instant_followup(
        "Chan", "BUY", _PARSED_2TP, "tg-3", bridge,
    ))
    assert result is True
    assert _trade_dict("trade-abc")["stop_loss"] == 2400.0


# ── ime_timeout_watchdog ─────────────────────────────────────────────────────

def _insert_stale(trade_id, strategy="scale_out", mt5_ticket=777,
                  entry_price=2415.0, stop_loss=2403.0, age_s=200, managed_by="python"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy, managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, "BUY", entry_price, entry_price,
             entry_price, 0.10, 0.10, stop_loss, "open", time.time() - age_s, strategy, managed_by),
        )


def test_watchdog_ignores_young_trade(fresh_db):
    _insert_stale("t-young", age_s=60)
    tick = SimpleNamespace(bid=2416.0, ask=2416.5)
    asyncio.run(followup.ime_timeout_watchdog(tick, _FakeBridge()))
    assert _trade_dict("t-young")["tp1"] is None


def test_watchdog_skips_self_managing_strategy(fresh_db):
    _insert_stale("t-cons", strategy="conservative", age_s=200)
    tick = SimpleNamespace(bid=2416.0, ask=2416.5)
    asyncio.run(followup.ime_timeout_watchdog(tick, _FakeBridge()))
    trade = _trade_dict("t-cons")
    assert trade["tp1"] is None
    assert trade["stop_loss"] == 2403.0


def test_watchdog_assigns_tps_sl_unchanged_when_tp1_not_cleared(fresh_db):
    _insert_stale("t-1", age_s=200)
    tick = SimpleNamespace(bid=2416.0, ask=2416.5)
    bridge = _FakeBridge()
    asyncio.run(followup.ime_timeout_watchdog(tick, bridge))
    trade = _trade_dict("t-1")
    assert [trade[f"tp{i}"] for i in range(1, 7)] == [2418.0, 2420.0, 2422.0, 2425.0, 2429.0, 2433.0]
    assert trade["stop_loss"] == 2403.0
    assert trade["sl_moved_to_be"] == 0
    assert bridge.modify_order_calls == [{"ticket": 777, "sl": 2403.0, "tp": None}]


def test_watchdog_moves_sl_to_be_when_tp1_already_cleared(fresh_db):
    _insert_stale("t-2", age_s=200)
    tick = SimpleNamespace(bid=2419.0, ask=2419.5)
    bridge = _FakeBridge()
    asyncio.run(followup.ime_timeout_watchdog(tick, bridge))
    trade = _trade_dict("t-2")
    assert trade["stop_loss"] == 2415.0
    assert trade["sl_moved_to_be"] == 1
    assert bridge.modify_order_calls == [{"ticket": 777, "sl": 2415.0, "tp": None}]


def test_watchdog_skips_ea_managed_trade_when_ea_healthy(fresh_db, monkeypatch):
    """EA-managed trades (built-in ladder strategies and EA Templates
    alike) are protected by the EA's own on-tick logic -- confirmed live
    2026-07-23 that without this guard, a template with tpsl_mode="off"
    (no TP by design) got hijacked after 3 minutes: a generic fallback TP
    ladder was force-assigned and SL was moved to breakeven via a raw MT5
    modify_order call, undermining the template's own management."""
    _insert_stale("t-ea", strategy="template:StealthTest", managed_by="ea", age_s=200)
    tick = SimpleNamespace(bid=2419.0, ask=2419.5)  # would otherwise clear auto-TP1
    bridge = _FakeBridge()

    class _FakeEA:
        def is_ea_healthy(self):
            return True

    from backend.src.services.broker import ea_bridge as ea_bridge
    monkeypatch.setattr(ea_bridge, "_instance", _FakeEA())
    asyncio.run(followup.ime_timeout_watchdog(tick, bridge))
    trade = _trade_dict("t-ea")
    assert trade["tp1"] is None
    assert trade["stop_loss"] == 2403.0
    assert trade["sl_moved_to_be"] == 0
    assert bridge.modify_order_calls == []


def test_watchdog_falls_through_for_ea_managed_trade_when_ea_unhealthy(fresh_db, monkeypatch):
    """If the EA isn't there to protect it, the watchdog's generic
    fallback is better than leaving the trade completely unmanaged."""
    _insert_stale("t-ea-down", strategy="template:StealthTest", managed_by="ea", age_s=200)
    tick = SimpleNamespace(bid=2416.0, ask=2416.5)
    bridge = _FakeBridge()

    class _FakeEA:
        def is_ea_healthy(self):
            return False

    from backend.src.services.broker import ea_bridge as ea_bridge
    monkeypatch.setattr(ea_bridge, "_instance", _FakeEA())
    asyncio.run(followup.ime_timeout_watchdog(tick, bridge))
    trade = _trade_dict("t-ea-down")
    assert trade["tp1"] == 2418.0
