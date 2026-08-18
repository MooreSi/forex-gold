"""A stop-out books at the stop, not at whatever tick the poll caught.

The outcome loop runs every 5s and the SL branch used to exit at the CURRENT
tick, so every point gold travelled between the stop being touched and the next
poll was charged to the trade as if it had sat there unprotected. Across 605
closed losing signals, 84% came in worse than -1.0R (worst -5.75R), dragging
the average loss to -1.263R against +0.406R average wins: a 0.32:1 payoff
needing a 75.7% win rate to break even, against the 70.5% actually achieved.

The model trains on those labels, so it was being taught these trades lose by
more than a stopped-out trade really loses.

Slippage is still allowed -- spread plus a bounded amount -- because real stops
do slip. What is not allowed is charging polling latency as slippage.
"""
from types import SimpleNamespace

import pytest

from forex_trader.reversal_engine.reversal_engine_manage import (
    _STOP_SLIP_MAX_PTS,
    _ManagementMixin,
)


@pytest.fixture
def mgr():
    return _ManagementMixin()


def _tick(bid, spread=0.30):
    return SimpleNamespace(bid=bid, ask=bid + spread, mid=bid + spread / 2)


# ── the regression ───────────────────────────────────────────────────────────

def test_buy_stop_does_not_charge_a_crash_that_happened_between_polls(mgr):
    """The -5.75R shape: stop at 3990, poll caught price at 3980."""
    t = _tick(3980.0)
    assert mgr._realistic_fill(t, "BUY", True) == pytest.approx(3980.0)
    filled = mgr._stop_fill(t, "BUY", 3990.0)
    assert filled == pytest.approx(3990.0 - (0.30 + _STOP_SLIP_MAX_PTS))
    assert filled > 3989.0, "still booking the whole excursion"


def test_sell_stop_does_not_charge_a_spike_that_happened_between_polls(mgr):
    t = _tick(4020.0)
    filled = mgr._stop_fill(t, "SELL", 4010.0)
    assert filled == pytest.approx(4010.0 + (0.30 + _STOP_SLIP_MAX_PTS))


@pytest.mark.parametrize("gap", [1, 5, 20, 100])
def test_the_loss_is_bounded_however_far_price_ran(mgr, gap):
    """A 100-point gap and a 1-point one must book the same stop-out: the
    difference between them is latency, not risk the trade took on."""
    sl = 4000.0
    filled = mgr._stop_fill(_tick(sl - gap), "BUY", sl)
    assert sl - filled <= 0.30 + _STOP_SLIP_MAX_PTS + 1e-9


# ── what it must still allow ─────────────────────────────────────────────────

def test_slippage_inside_the_allowance_is_kept(mgr):
    """Real stops slip. Booking every stop exactly at its level would flatter
    the label in the other direction."""
    t = _tick(3989.60)
    assert mgr._stop_fill(t, "BUY", 3990.0) == pytest.approx(3989.60)


def test_a_wider_spread_widens_the_allowance(mgr):
    """Crossing the spread is a genuine cost of exiting, so it scales with the
    spread actually quoted rather than a fixed guess."""
    narrow = mgr._stop_fill(_tick(3900.0, spread=0.10), "BUY", 3990.0)
    wide = mgr._stop_fill(_tick(3900.0, spread=2.00), "BUY", 3990.0)
    assert wide < narrow


# ── it must not flatter the trade either ─────────────────────────────────────

def test_a_recovered_price_still_fills_at_the_stop(mgr):
    """Once touched, a stop is filled. If price came back before the next poll,
    the trade does not get to keep the better price."""
    assert mgr._stop_fill(_tick(3995.0), "BUY", 3990.0) == pytest.approx(3990.0)


def test_sell_side_recovery_also_fills_at_the_stop(mgr):
    assert mgr._stop_fill(_tick(4005.0), "SELL", 4010.0) == pytest.approx(4010.0)


def test_fill_is_never_on_the_profitable_side_of_the_stop(mgr):
    for price in (3980.0, 3989.9, 3990.0, 3995.0, 4100.0):
        assert mgr._stop_fill(_tick(price), "BUY", 3990.0) <= 3990.0 + 1e-9
    for price in (4020.0, 4010.1, 4010.0, 4005.0, 3900.0):
        assert mgr._stop_fill(_tick(price), "SELL", 4010.0) >= 4010.0 - 1e-9


# ── robustness ───────────────────────────────────────────────────────────────

def test_a_tick_with_no_usable_spread_still_books_a_stop(mgr):
    """This runs on every stop-out; it must not be able to raise and leave the
    signal open and unaccounted."""
    bad = SimpleNamespace(bid=3980.0, ask=None, mid=3980.0)
    assert mgr._stop_fill(bad, "BUY", 3990.0) == pytest.approx(3990.0 - _STOP_SLIP_MAX_PTS)


def test_both_stop_exits_in_the_conservative_path_pass_their_stop_level():
    """The fixed stop and the trail are both stop-type exits, so both have to
    book at their own level -- the trail especially, since it is the profitable
    exit and booking it late gives back the runner's gains."""
    import inspect
    from forex_trader.reversal_engine import reversal_engine_manage as mod
    src = inspect.getsource(mod)
    assert '_close_remaining("loss", fixed_sl)' in src
    assert '_close_remaining("win", current_sl)' in src


def test_the_ladder_stop_branch_uses_the_stop_fill():
    import inspect
    from forex_trader.reversal_engine import reversal_engine_manage as mod
    src = inspect.getsource(mod)
    assert "exit_fill = self._stop_fill(tick, direction, sl)" in src
