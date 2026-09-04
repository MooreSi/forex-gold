"""Covers get_strategy_ladder_reach — how far up its TP ladder each strategy's
trades ACTUALLY get, as opposed to how far its configuration says they could.

Why it exists, from a real misjudgement on 2026-09-04. The AI Analysis tab
recommended "GD VIP - Single" on the reasoning that its eight-rung ladder
(20/40/60/80/100/120/170/270 pips) would "capture a continued move" and "let
profits run". Its own last 30 days said otherwise: of 85 closed trades, 50
(59%) topped out at TP1 or TP2 and exactly 3 ever reached TP5 or beyond. One
trade in 85 completed the ladder. The template banks a rung and is then
trailed out -- its trail arms at 40 pips (TP2) at a distance of 50 pips, and
50 pips is 42% of a typical H1 range on this instrument, so the upper rungs
are unreachable in practice rather than merely unlikely.

The prompt had no way to know any of that: it was handed the ladder as a
specification and reasoned about it on paper. This aggregate is the measured
counterweight -- the same idea as get_channel_strategy_breakdown, applied to
ladder depth instead of channel attribution.
"""
import time

import pytest


def _signal(conn, trade_id, created_at):
    """vantage_simulated_trades.signal_id is a FK -- the parent row must exist."""
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (f"sig-{trade_id}", "BUY", 2399.0, 2401.0, 2395.0, "filled", created_at),
    )


def _trade(conn, trade_id, strategy, *, max_tp_hit, exit_reason="SL",
           pnl=10.0, close_time=None, status="closed"):
    close_time = time.time() if close_time is None else close_time
    _signal(conn, trade_id, close_time - 120)
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, close_time, close_price, "
        " net_pnl, strategy, max_tp_hit, exit_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, f"sig-{trade_id}", 1000, "BUY", 2399.0, 2401.0, 2400.0,
         0.1, 0.1, 2395.0, status, close_time - 60, close_time, 2410.0,
         pnl, strategy, max_tp_hit, exit_reason),
    )


def test_reach_buckets_split_the_ladder_by_depth(fresh_db):
    """The headline number. A ladder whose trades stop at TP1/TP2 is a
    different instrument from one that runs to TP8, and the configuration
    alone cannot tell the two apart."""
    now = time.time()
    with fresh_db.db() as conn:
        for i, rung in enumerate(["TP1", "TP1", "TP2"]):
            _trade(conn, f"low{i}", "template:GD VIP - Single", max_tp_hit=rung)
        for i, rung in enumerate(["TP3", "TP4"]):
            _trade(conn, f"mid{i}", "template:GD VIP - Single", max_tp_hit=rung)
        _trade(conn, "high0", "template:GD VIP - Single", max_tp_hit="TP7")
        for i in range(2):
            _trade(conn, f"nil{i}", "template:GD VIP - Single",
                   max_tp_hit="none", pnl=-60.0)

    row = fresh_db.get_strategy_ladder_reach(min_n=1)["template:GD VIP - Single"]

    assert row["n"] == 8
    assert row["no_tp"] == 2
    assert row["tp1_2"] == 3
    assert row["tp3_4"] == 2
    assert row["tp5_plus"] == 1


@pytest.mark.parametrize("blank", ["none", "n/a", "", None])
def test_every_no_tp_spelling_counts_as_no_tp(fresh_db, blank):
    """The column carries 'none', 'n/a', '' and NULL for the same outcome.
    Treating any of them as a reached rung would overstate ladder depth,
    which is the one number this exists to make trustworthy."""
    now = time.time()
    with fresh_db.db() as conn:
        _trade(conn, "a", "s", max_tp_hit=blank, pnl=-60.0)

    row = fresh_db.get_strategy_ladder_reach(min_n=1)["s"]
    assert row["no_tp"] == 1
    assert row["tp1_2"] == 0


def test_stopped_after_a_rung_is_counted_separately(fresh_db):
    """The truncation signature: a TP was banked and the trade then exited on
    a stop. That is what a trail armed inside the ladder looks like in the
    data, and it is invisible in win rate -- these are WINS."""
    now = time.time()
    with fresh_db.db() as conn:
        # banked a rung, then trailed/stopped out
        for i in range(4):
            _trade(conn, f"trailed{i}", "s", max_tp_hit="TP2",
                   exit_reason="SL", pnl=30.0)
        # ran the whole ladder
        _trade(conn, "full", "s", max_tp_hit="TP8",
               exit_reason="all_tps_hit", pnl=200.0)
        # never got a rung -- a plain loser, not a truncation
        _trade(conn, "loser", "s", max_tp_hit="none",
               exit_reason="SL", pnl=-60.0)

    row = fresh_db.get_strategy_ladder_reach(min_n=1)["s"]
    assert row["stopped_after_tp"] == 4
    assert row["n"] == 6


def test_win_rate_and_pnl_come_from_the_same_rows(fresh_db):
    """The buckets are only persuasive next to the money. A split computed
    over a different row set than the P&L would be worse than no split."""
    now = time.time()
    with fresh_db.db() as conn:
        _trade(conn, "w1", "s", max_tp_hit="TP1", pnl=20.0)
        _trade(conn, "w2", "s", max_tp_hit="TP2", pnl=30.0)
        _trade(conn, "be", "s", max_tp_hit="none", pnl=0.0)
        _trade(conn, "l1", "s", max_tp_hit="none", pnl=-25.0)

    row = fresh_db.get_strategy_ladder_reach(min_n=1)["s"]
    assert row["n"] == 4
    assert row["net_pnl"] == 25.0
    # a scratch close is not a win
    assert row["win_rate"] == 50.0


def test_thin_samples_are_dropped(fresh_db):
    """A two-trade ladder distribution is noise, and presenting it beside an
    85-trade one invites exactly the over-reading this is meant to prevent."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(6):
            _trade(conn, f"big{i}", "busy", max_tp_hit="TP1")
        for i in range(2):
            _trade(conn, f"tiny{i}", "rare", max_tp_hit="TP8")

    rows = fresh_db.get_strategy_ladder_reach(min_n=5)
    assert "busy" in rows
    assert "rare" not in rows


def test_trades_outside_the_window_are_excluded(fresh_db):
    """A template retired six weeks ago should not be arguing about today's
    selection -- and its ladder depth was measured in a different market."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(3):
            _trade(conn, f"new{i}", "current", max_tp_hit="TP1", close_time=now)
        for i in range(3):
            _trade(conn, f"old{i}", "retired", max_tp_hit="TP8",
                   close_time=now - 45 * 86400)

    rows = fresh_db.get_strategy_ladder_reach(days=30, min_n=1)
    assert "current" in rows
    assert "retired" not in rows


def test_open_trades_are_not_counted(fresh_db):
    """An open trade has not finished climbing. Counting its current rung as
    its final one would systematically understate ladder depth."""
    now = time.time()
    with fresh_db.db() as conn:
        for i in range(3):
            _trade(conn, f"c{i}", "s", max_tp_hit="TP1")
        _trade(conn, "still_running", "s", max_tp_hit="TP1",
               status="open", exit_reason=None)

    row = fresh_db.get_strategy_ladder_reach(min_n=1)["s"]
    assert row["n"] == 3


def test_empty_database_returns_no_rows_rather_than_raising(fresh_db):
    """This feeds a prompt. It must never be the reason an analysis fails."""
    assert fresh_db.get_strategy_ladder_reach() == {}
