"""Covers get_channel_strategy_breakdown (core/core_db_channel.py) — the
per-strategy split of a channel's record that the AI evaluator reads.

Why it exists, from a real misjudgement on 2026-08-16: GOLD DIGGERS
INSTITUTIONAL reached the evaluator as a single line, "WR=50.0% n=90
PnL=$-1465.34", and was stood down for having "no edge". The same 30 days,
split by the strategy each trade actually ran under, said something else
entirely -- 76.5% WR and +$228 across 34 limit-entry trades, with the whole
loss sitting in two wide-stop templates (Staged Ratchet 100-500 at 13% WR
/ -$1087, Asian Reversal - ATR at 33% / -$560) that do not resemble the
geometry the channel was about to be given. The aggregate could not
distinguish "this channel has no edge" from "this channel was run on the
wrong geometry for two days", so the evaluator picked the wrong one --
against its own backtested map, which rates that channel positive in every
regime.
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db


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


def _signal(conn, trade_id, created_at):
    """vantage_simulated_trades.signal_id is a FK -- the parent row must exist."""
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (f"sig-{trade_id}", "BUY", 2399.0, 2401.0, 2395.0, "filled", created_at),
    )


def _trade(conn, trade_id, tg_source, strategy, pnl, close_time):
    _signal(conn, trade_id, close_time - 120)
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, close_time, close_price, "
        " net_pnl, tg_source, strategy) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, f"sig-{trade_id}", 1000, "BUY", 2399.0, 2401.0, 2400.0,
         0.1, 0.1, 2395.0, "closed", close_time - 60, close_time, 2410.0,
         pnl, tg_source, strategy),
    )


def _by_strategy(rows):
    return {r["strategy"]: r for r in rows}


def test_split_separates_a_losing_config_from_a_profitable_channel(fresh_db):
    """The headline case: aggregate says -$900 across 14 trades, but the
    channel is profitable on limit entries and the loss is one template."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(8):
            _trade(conn, f"win{i}", "Gold Diggers VIP", "limit_runner", 25.0, now)
        for i in range(6):
            _trade(conn, f"loss{i}", "Gold Diggers VIP",
                   "template:Staged Ratchet 100-500", -183.0, now)

    rows = _by_strategy(fresh_db.get_channel_strategy_breakdown()["Gold Diggers VIP"])

    assert rows["limit_runner"]["n"] == 8
    assert rows["limit_runner"]["win_rate"] == 100.0
    assert rows["limit_runner"]["net_pnl"] == 200.0

    ratchet = rows["template:Staged Ratchet 100-500"]
    assert ratchet["n"] == 6
    assert ratchet["win_rate"] == 0.0
    assert ratchet["net_pnl"] == -1098.0


def test_win_rate_counts_a_zero_pnl_trade_as_not_a_win(fresh_db):
    """A scratch/breakeven close is not a win -- counting it as one would
    inflate exactly the number this split exists to make trustworthy."""
    now = time.time()
    with fresh_db.db() as conn:
        _trade(conn, "a", "Gold Diggers VIP", "conservative", 10.0, now)
        _trade(conn, "b", "Gold Diggers VIP", "conservative", 0.0, now)
        _trade(conn, "c", "Gold Diggers VIP", "conservative", -5.0, now)

    row = _by_strategy(fresh_db.get_channel_strategy_breakdown(min_n=1)["Gold Diggers VIP"])
    assert row["conservative"]["n"] == 3
    assert row["conservative"]["win_rate"] == pytest.approx(33.3)


def test_thin_samples_are_dropped(fresh_db):
    """A one-trade strategy is noise. Surfacing it as a row invites the same
    over-reading of a tiny sample that the aggregate already caused."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(5):
            _trade(conn, f"m{i}", "Gold Diggers VIP", "conservative", 5.0, now)
        _trade(conn, "single", "Gold Diggers VIP", "signal_climber", 500.0, now)

    rows = _by_strategy(fresh_db.get_channel_strategy_breakdown(min_n=3)["Gold Diggers VIP"])
    assert "conservative" in rows
    assert "signal_climber" not in rows


def test_rows_are_ranked_by_sample_size_and_capped(fresh_db):
    now = time.time()
    with fresh_db.db() as conn:
        for name, count in (("s_big", 9), ("s_mid", 7), ("s_small", 5), ("s_tiny", 3)):
            for i in range(count):
                _trade(conn, f"{name}{i}", "Gold Diggers VIP", name, 1.0, now)

    rows = fresh_db.get_channel_strategy_breakdown(min_n=3, top_n=2)["Gold Diggers VIP"]
    assert [r["strategy"] for r in rows] == ["s_big", "s_mid"]


def test_variant_source_names_merge_into_the_canonical_channel(fresh_db):
    """Trades arrive under decorated sources ("Telegram Auto (X)"). Splitting
    those into separate channels would halve every sample and hide the very
    concentration this is meant to expose."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(3):
            _trade(conn, f"p{i}", "GOLD DIGGERS INSTITUTIONAL", "limit_runner", 10.0, now)
        for i in range(3):
            _trade(conn, f"v{i}", "Telegram Auto (GOLD DIGGERS INSTITUTIONAL)",
                   "limit_runner", 10.0, now)

    result = fresh_db.get_channel_strategy_breakdown()
    assert "Telegram Auto (GOLD DIGGERS INSTITUTIONAL)" not in result
    rows = _by_strategy(result["GOLD DIGGERS INSTITUTIONAL"])
    assert rows["limit_runner"]["n"] == 6


def test_trades_outside_the_window_are_excluded(fresh_db):
    """The window is what makes this current. A config retired six weeks ago
    should not still be arguing about today's selection."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(4):
            _trade(conn, f"recent{i}", "Gold Diggers VIP", "conservative", 5.0, now)
        for i in range(4):
            _trade(conn, f"old{i}", "Gold Diggers VIP", "ancient_strategy",
                   -500.0, now - 45 * 86400)

    rows = _by_strategy(fresh_db.get_channel_strategy_breakdown(days=30)["Gold Diggers VIP"])
    assert "conservative" in rows
    assert "ancient_strategy" not in rows


def test_open_trades_are_not_counted(fresh_db):
    """An open trade has no realised result; including it would let a
    currently-underwater position vote on strategy selection."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(3):
            _trade(conn, f"c{i}", "Gold Diggers VIP", "conservative", 5.0, now)
        _signal(conn, "open1", now - 120)
        conn.execute(
            "INSERT INTO vantage_simulated_trades "
            "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
            " entry_price, lot_size, remaining_lots, stop_loss, status, open_time, "
            " net_pnl, tg_source, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("open1", "sig-open1", 1001, "BUY", 2399.0, 2401.0, 2400.0, 0.1, 0.1,
             2395.0, "open", now - 60, -999.0, "Gold Diggers VIP", "conservative"),
        )

    rows = _by_strategy(fresh_db.get_channel_strategy_breakdown()["Gold Diggers VIP"])
    assert rows["conservative"]["n"] == 3
    assert rows["conservative"]["net_pnl"] == 15.0


def test_empty_database_returns_no_rows_rather_than_raising(fresh_db):
    """This feeds a prompt on a 15-minute loop -- it must never be the reason
    the evaluator fails to run."""
    assert fresh_db.get_channel_strategy_breakdown() == {}
