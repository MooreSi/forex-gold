"""How a Trend PA trade ends, and what the panel says about a set of them.

Pure arithmetic over dicts: no broker, no database.
"""
from __future__ import annotations

import pytest

from backend.src.services.trend_pa import outcome as oc
from backend.src.services.trend_pa import stats as ss


def _b(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


# ── outcome on bars (the backtest) ───────────────────────────────────────────

def test_a_buy_that_reaches_its_target_wins_at_the_target():
    r = oc.resolve_bars("BUY", 100, 98, 104, [_b(1, 100, 101, 99, 100.5), _b(2, 100.5, 104.5, 100, 104)])
    assert r == {"outcome": "win", "exit_price": 104, "exit_ts": 2}


def test_a_sell_that_reaches_its_stop_loses_at_the_stop():
    r = oc.resolve_bars("SELL", 100, 102, 96, [_b(1, 100, 102.5, 99, 102)])
    assert r == {"outcome": "loss", "exit_price": 102, "exit_ts": 1}


def test_when_one_bar_spans_both_the_stop_comes_first():
    """Nothing in a single bar says which was touched first, so the backtest
    assumes the worse -- same rule as backtest/engine and breakout/backtest."""
    r = oc.resolve_bars("BUY", 100, 98, 104, [_b(1, 100, 105, 97, 101)])
    assert r["outcome"] == "loss"


def test_a_trade_still_open_at_the_end_of_the_bars_is_open():
    assert oc.resolve_bars("BUY", 100, 98, 104, [_b(1, 100, 101, 99, 100)]) is None


def test_a_trade_past_its_maximum_hold_closes_at_the_bar_close():
    bars = [_b(10, 100, 101, 99, 100.5), _b(20, 100.5, 101, 99.5, 100.8)]
    r = oc.resolve_bars("BUY", 100, 98, 104, bars, opened_ts=0, max_hold_s=15)
    assert r == {"outcome": "timeout", "exit_price": 100.8, "exit_ts": 20}


def test_a_trade_inside_its_maximum_hold_is_not_timed_out():
    bars = [_b(10, 100, 101, 99, 100.5)]
    assert oc.resolve_bars("BUY", 100, 98, 104, bars, opened_ts=0, max_hold_s=15) is None


# ── outcome on a tick (live) ─────────────────────────────────────────────────

@pytest.mark.parametrize("direction,bid,ask,expected", [
    ("BUY", 97.9, 98.2, "loss"),    # a buy closes on the bid
    ("BUY", 104.0, 104.3, "win"),
    ("BUY", 99.0, 104.1, None),     # the ASK at target is not a buy's exit
    ("SELL", 101.8, 102.0, "loss"),  # a sell closes on the ask
    ("SELL", 95.7, 96.0, "win"),
    ("SELL", 95.0, 97.0, None),
])
def test_tick_exits(direction, bid, ask, expected):
    sl, tp = (98, 104) if direction == "BUY" else (102, 96)
    assert oc.resolve_tick(direction, sl, tp, bid, ask) == expected


def test_r_multiple_charges_the_cost():
    assert oc.r_multiple("BUY", 100, 104, 2, cost=0.0) == pytest.approx(2.0)
    assert oc.r_multiple("BUY", 100, 104, 2, cost=0.3) == pytest.approx(1.85)
    assert oc.r_multiple("SELL", 100, 102, 2, cost=0.3) == pytest.approx(-1.15)


# ── stats ────────────────────────────────────────────────────────────────────

def _row(r, outcome, ts, session="london", pattern="pin", direction="BUY"):
    return {"r_net": r, "outcome": outcome, "closed_at": ts,
            "session": session, "pattern": pattern, "direction": direction}


ROWS = [
    _row(2.0, "win", 1),
    _row(-1.0, "loss", 2, session="overlap"),
    _row(-1.0, "loss", 3, pattern="engulfing"),
    _row(1.9, "win", 4, direction="SELL"),
    _row(-0.4, "timeout", 5),
]


def test_empty_is_honest():
    s = ss.summarize([], rr=2.0)
    assert s["n"] == 0 and s["win_rate"] is None and s["profit_factor"] is None
    assert s["balance"] == pytest.approx(1000.0)


def test_headline_figures():
    s = ss.summarize(ROWS, rr=2.0)
    assert (s["n"], s["wins"], s["losses"], s["timeouts"]) == (5, 2, 2, 1)
    assert s["win_rate"] == pytest.approx(0.4)
    assert s["total_r"] == pytest.approx(1.5)
    assert s["avg_r"] == pytest.approx(0.3)
    assert s["profit_factor"] == pytest.approx(3.9 / 2.4)


def test_break_even_win_rate_is_the_geometry():
    """At 1:2 a no-edge entry wins a third of the time. Win rate means nothing
    without that line beside it (engines README, 2026-09-23)."""
    assert ss.summarize(ROWS, rr=2.0)["breakeven_win_rate"] == pytest.approx(1 / 3)


def test_drawdown_in_r_is_peak_to_trough_of_the_running_total():
    # running: 2, 1, 0, 1.9, 1.5 -> peak 2, trough 0
    assert ss.summarize(ROWS, rr=2.0)["max_drawdown_r"] == pytest.approx(2.0)


def test_the_virtual_balance_compounds_one_percent_risk():
    s = ss.summarize([_row(2.0, "win", 1), _row(2.0, "win", 2)], rr=2.0)
    assert s["balance"] == pytest.approx(1000 * 1.02 * 1.02)


def test_rows_are_taken_in_close_order_whatever_order_they_arrive_in():
    assert ss.summarize(list(reversed(ROWS)), rr=2.0)["max_drawdown_r"] == pytest.approx(2.0)


def test_splits_carry_their_own_counts():
    s = ss.summarize(ROWS, rr=2.0)
    by = {r["key"]: r for r in s["by_session"]}
    assert by["london"]["n"] == 4 and by["overlap"]["n"] == 1
    assert by["overlap"]["total_r"] == pytest.approx(-1.0)
    assert {r["key"] for r in s["by_pattern"]} == {"pin", "engulfing"}
    assert {r["key"] for r in s["by_direction"]} == {"BUY", "SELL"}


def test_no_losses_means_no_profit_factor_rather_than_infinity():
    assert ss.summarize([_row(2.0, "win", 1)], rr=2.0)["profit_factor"] is None
