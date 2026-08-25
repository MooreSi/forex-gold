"""Internal Engine Exposure guard (Trading > Strategy) -- applies only to
the internal signal generators, never to Telegram-channel trades. Off by
default. See core_internal_exposure_guard.py for why.
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.services.positions import core_internal_exposure_guard as guard


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    os.remove(path)


def _open_trade(tid, direction, lots, source, status="open"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{tid}", direction, 4000.0, 4000.0, 3990.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"sig-{tid}", 111, direction, 4000.0, 4000.0, 4000.0, lots, lots,
             3990.0, status, time.time(), "scale_out", source),
        )


# ── Off (default) ────────────────────────────────────────────────────────

def test_defaults_to_off(fresh_db):
    rs = db.get_risk_settings()
    assert (rs.get("internal_hedge_mode") or "off") == "off"


def test_off_allows_opposing(fresh_db):
    _open_trade("t1", "BUY", 0.10, "Reversal Engine")
    ok, reason = guard.check_internal_exposure("SELL", 0.10, {"internal_hedge_mode": "off"})
    assert ok
    assert reason == ""


# ── Self-hedge guard ─────────────────────────────────────────────────────

def test_self_hedge_blocks_opposing(fresh_db):
    _open_trade("t1", "BUY", 0.10, "Reversal Engine")
    ok, reason = guard.check_internal_exposure(
        "SELL", 0.10, {"internal_hedge_mode": "self_hedge"})
    assert not ok
    assert "Self-Hedge Guard" in reason


def test_self_hedge_allows_same_direction(fresh_db):
    _open_trade("t1", "BUY", 0.10, "Reversal Engine")
    ok, _ = guard.check_internal_exposure(
        "BUY", 0.10, {"internal_hedge_mode": "self_hedge"})
    assert ok


def test_self_hedge_spans_different_internal_engines(fresh_db):
    _open_trade("t1", "BUY", 0.10, "Breakout Engine")
    ok, _ = guard.check_internal_exposure(
        "SELL", 0.10, {"internal_hedge_mode": "self_hedge"})
    assert not ok


def test_self_hedge_ignores_closed_trades(fresh_db):
    _open_trade("t1", "BUY", 0.10, "Reversal Engine", status="closed")
    ok, _ = guard.check_internal_exposure(
        "SELL", 0.10, {"internal_hedge_mode": "self_hedge"})
    assert ok


# ── Telegram trades are out of scope ─────────────────────────────────────

def test_telegram_channel_trades_never_counted(fresh_db):
    _open_trade("t1", "BUY", 0.50, "GOLD DIGGERS INSTITUTIONAL")
    ok, _ = guard.check_internal_exposure(
        "SELL", 0.10, {"internal_hedge_mode": "self_hedge"})
    assert ok
    ok2, _ = guard.check_internal_exposure(
        "BUY", 0.10, {"internal_hedge_mode": "net_exposure",
                      "internal_net_exposure_max_lots": 0.30})
    assert ok2


def test_legacy_engine_source_names_still_counted(fresh_db):
    # Pre-rename tg_source still present on historical rows.
    _open_trade("t1", "BUY", 0.10, "Bounce Generator")
    ok, _ = guard.check_internal_exposure(
        "SELL", 0.10, {"internal_hedge_mode": "self_hedge"})
    assert not ok


# ── Net exposure cap ─────────────────────────────────────────────────────

_NET = {"internal_hedge_mode": "net_exposure", "internal_net_exposure_max_lots": 0.30}


def test_net_exposure_allows_up_to_cap(fresh_db):
    _open_trade("t1", "BUY", 0.20, "Reversal Engine")
    ok, _ = guard.check_internal_exposure("BUY", 0.10, _NET)   # -> +0.30
    assert ok


def test_net_exposure_blocks_over_cap(fresh_db):
    _open_trade("t1", "BUY", 0.30, "Reversal Engine")
    ok, reason = guard.check_internal_exposure("BUY", 0.10, _NET)  # -> +0.40
    assert not ok
    assert "Net Exposure Cap" in reason


def test_net_exposure_always_allows_a_reducing_hedge(fresh_db):
    # Well over the cap on the long side, but a SELL brings net back toward
    # flat -- must always be permitted.
    _open_trade("t1", "BUY", 0.30, "Reversal Engine")
    _open_trade("t2", "BUY", 0.30, "Reversal Engine")
    ok, _ = guard.check_internal_exposure("SELL", 0.10, _NET)
    assert ok


def test_net_exposure_symmetric_on_short_side(fresh_db):
    _open_trade("t1", "SELL", 0.30, "Reversal Engine")
    ok, _ = guard.check_internal_exposure("SELL", 0.10, _NET)   # -> -0.40
    assert not ok


def test_net_exposure_hedged_book_reads_flat(fresh_db):
    _open_trade("t1", "BUY", 0.30, "Reversal Engine")
    _open_trade("t2", "SELL", 0.30, "Reversal Engine")
    assert guard.net_internal_exposure() == 0.0
    ok, _ = guard.check_internal_exposure("BUY", 0.30, _NET)  # -> +0.30
    assert ok


def test_net_exposure_zero_cap_disables_the_check(fresh_db):
    _open_trade("t1", "BUY", 5.00, "Reversal Engine")
    ok, _ = guard.check_internal_exposure(
        "BUY", 5.00, {"internal_hedge_mode": "net_exposure",
                      "internal_net_exposure_max_lots": 0.0})
    assert ok


def test_net_exposure_uses_remaining_lots_not_original(fresh_db):
    _open_trade("t1", "BUY", 0.30, "Reversal Engine")
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET remaining_lots=0.10 WHERE trade_id='t1'")
    assert guard.net_internal_exposure() == 0.10


def test_unknown_mode_falls_back_to_allow(fresh_db):
    _open_trade("t1", "BUY", 0.10, "Reversal Engine")
    ok, _ = guard.check_internal_exposure("SELL", 0.10, {"internal_hedge_mode": "bogus"})
    assert ok
