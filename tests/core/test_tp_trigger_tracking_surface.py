"""Proves backend.src.services.positions.tp_tracking's extracted functions
behave identically to the SimulationEngine methods characterized in
test_tp_trigger_tracking_characterization.py -- see
docs/todo/refactor/core-tp-trigger-tracking-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class.
The SimulationEngine.__new__() instance is replaced by a plain TPCache().
Reuses the db-worker-thread reset helper discovered in 010 -- required for
any test exercising get_triggered_tps/check_tp_hits (both route through
db_module.to_db_thread()).
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.positions import tp_tracking as tp


@pytest.fixture
def cache(fresh_db):
    return tp.TPCache()


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", remaining_lots=0.10, **tps):
    with db.db() as conn:
        cols = "trade_id, signal_id, direction, entry_low, entry_high, entry_price, " \
               "lot_size, remaining_lots, stop_loss, status, open_time"
        vals = [trade_id, sig_id, direction, 2399.0, 2401.0, 2400.0, 0.10,
                remaining_lots, 2390.0, "open", time.time()]
        for k, v in tps.items():
            cols += f", {k}"
            vals.append(v)
        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({placeholders})", vals)


def _insert_partial_close(trade_id, reason, lots_closed=0.05, ts=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id, ts, lots_closed, close_price, pnl, reason) "
            "VALUES (?,?,?,?,?,?)",
            (trade_id, ts or time.time(), lots_closed, 2405.0, 25.0, reason),
        )


# ── get_triggered_tps ─────────────────────────────────────────────────────────

def test_get_triggered_tps_parses_reason_strings(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP3")

    triggered = asyncio.run(tp.get_triggered_tps(cache, "t-1"))
    assert triggered == {1, 3}


def test_get_triggered_tps_ignores_non_matching_reasons(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "manual_close")

    triggered = asyncio.run(tp.get_triggered_tps(cache, "t-1"))
    assert triggered == set()


def test_get_triggered_tps_ttl_cache_returns_stale_within_window(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1")

    first = asyncio.run(tp.get_triggered_tps(cache, "t-1"))
    assert first == {1}

    _insert_partial_close("t-1", "TP2")
    second = asyncio.run(tp.get_triggered_tps(cache, "t-1"))
    assert second == {1}


def test_get_triggered_tps_reloads_after_ttl_expiry(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1")

    asyncio.run(tp.get_triggered_tps(cache, "t-1"))

    _insert_partial_close("t-1", "TP2")
    cached_set, _ = cache.triggered["t-1"]
    cache.triggered["t-1"] = (cached_set, time.time() - 10)
    reloaded = asyncio.run(tp.get_triggered_tps(cache, "t-1"))
    assert reloaded == {1, 2}


# ── last_closed_tp ─────────────────────────────────────────────────────────────

def test_last_closed_tp_returns_most_recent(fresh_db):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1", ts=100.0)
    _insert_partial_close("t-1", "TP2", ts=200.0)

    result = tp.last_closed_tp("t-1")
    assert result == 2


def test_last_closed_tp_ignores_zero_lot_rows(fresh_db):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP2", lots_closed=0.05, ts=100.0)
    _insert_partial_close("t-1", "TP3", lots_closed=0.0, ts=200.0)

    result = tp.last_closed_tp("t-1")
    assert result == 2


def test_last_closed_tp_returns_none_when_no_match(fresh_db):
    _insert_signal()
    _insert_trade("t-1")
    result = tp.last_closed_tp("t-1")
    assert result is None


# ── log_tp_wait_diagnostic ─────────────────────────────────────────────────────

def test_log_tp_wait_diagnostic_does_not_raise(fresh_db, cache):
    tp.log_tp_wait_diagnostic(
        cache, "t-1", "TP1_WAIT", "BUY", current_price=2400.0, target_price=2410.0, hit=False,
    )
    tp.log_tp_wait_diagnostic(
        cache, "t-1", "TP1_WAIT", "SELL", current_price=2400.0, target_price=2390.0, hit=True,
    )
    assert "t-1" in cache.wait_log_ts


# ── check_tp_hits ──────────────────────────────────────────────────────────────

def test_check_tp_hits_buy_single_hit(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1", tp1=2410.0, tp2=2420.0)
    trade = {"trade_id": "t-1", "direction": "BUY", "entry_price": 2400.0,
             "tp1": 2410.0, "tp2": 2420.0}
    hits = asyncio.run(tp.check_tp_hits(cache, trade, _tick(bid=2415.0, ask=2415.5)))
    assert hits == [("t-1", 1)]


def test_check_tp_hits_skips_already_triggered(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1")
    trade = {"trade_id": "t-1", "direction": "BUY", "entry_price": 2400.0,
             "tp1": 2410.0, "tp2": 2420.0}
    hits = asyncio.run(tp.check_tp_hits(cache, trade, _tick(bid=2415.0, ask=2415.5)))
    assert hits == []


def test_check_tp_hits_skips_tp_on_wrong_side_of_entry(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1")
    trade = {"trade_id": "t-1", "direction": "BUY", "entry_price": 2400.0, "tp1": 2395.0}
    hits = asyncio.run(tp.check_tp_hits(cache, trade, _tick(bid=2415.0, ask=2415.5)))
    assert hits == []


def test_check_tp_hits_sell_direction(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1", direction="SELL")
    trade = {"trade_id": "t-1", "direction": "SELL", "entry_price": 2400.0, "tp1": 2390.0}
    hits = asyncio.run(tp.check_tp_hits(cache, trade, _tick(bid=2389.5, ask=2389.0)))
    assert hits == [("t-1", 1)]


def test_check_tp_hits_multiple_simultaneous(fresh_db, cache):
    _insert_signal()
    _insert_trade("t-1")
    trade = {"trade_id": "t-1", "direction": "BUY", "entry_price": 2400.0,
             "tp1": 2405.0, "tp2": 2410.0, "tp3": 2415.0}
    hits = asyncio.run(tp.check_tp_hits(cache, trade, _tick(bid=2420.0, ask=2420.5)))
    assert hits == [("t-1", 1), ("t-1", 2), ("t-1", 3)]


# ── get_remaining_lots ─────────────────────────────────────────────────────────

def test_get_remaining_lots_reads_current_value(fresh_db):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.07)
    result = tp.get_remaining_lots("t-1")
    assert result == 0.07


def test_get_remaining_lots_returns_zero_for_unknown_trade(fresh_db):
    result = tp.get_remaining_lots("does-not-exist")
    assert result == 0.0
