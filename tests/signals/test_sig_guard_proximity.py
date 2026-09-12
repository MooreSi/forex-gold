"""Sig Guard's proximity half: don't stack on the same level, do allow a
genuinely separate setup.

`_sig_guard_blocks` refuses a new template trade when one is already open for
the same channel and direction. With `guard_pips > 0` it only refuses when the
existing entry is within that many pips of the new one — the difference between
"never stack on this channel" and the reference copier's "SIG GUARD: 20p".

The on/off half is covered end to end in
`tests/core/test_signal_resolution_surface.py`. The proximity half had no test
at all, and it is the half that actually fires: the live account's
`live_exec_status` carries refusals like *"a template-managed trade is already
open for 'Reversal Engine' SELL within 25 pips of $4349.50"*.

These drive the decision directly with a stubbed repo read, so each branch is
one case rather than a database fixture.
"""
from __future__ import annotations

import pytest

from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD
from backend.src.services.signals import resolution as sr


@pytest.fixture
def open_entries(monkeypatch):
    """Whatever this returns is "what is already open for this channel"."""
    def _set(entries):
        monkeypatch.setattr(sr.signals_repo, "template_trade_open_entries",
                            lambda *a, **k: list(entries))
    return _set


class TestWithNothingOpen:
    def test_nothing_open_never_blocks(self, open_entries):
        open_entries([])

        assert sr._sig_guard_blocks("Chan", "BUY") is False

    def test_nothing_open_never_blocks_even_with_a_proximity_window(self, open_entries):
        open_entries([])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4300.0) is False


class TestTheAllOrNothingMode:
    def test_zero_pips_blocks_on_any_open_trade_however_far_away(self, open_entries):
        """The original behaviour and still the default: one open template
        trade on this channel and direction is enough."""
        open_entries([9999.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 0.0, 4300.0) is True

    def test_no_entry_price_falls_back_to_all_or_nothing(self, open_entries):
        """A proximity window is meaningless with nothing to measure from, and
        the fallback is the strict answer rather than the permissive one."""
        open_entries([9999.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, None) is True


class TestTheProximityWindow:
    def test_an_entry_inside_the_window_blocks(self, open_entries):
        open_entries([4300.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4301.0) is True

    def test_an_entry_outside_the_window_is_allowed(self, open_entries):
        """The whole point of the pips form: a separate setup further down the
        chart is a different trade, not a stack."""
        open_entries([4300.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4310.0) is False

    def test_exactly_on_the_boundary_blocks(self, open_entries):
        """20 pips is 2.00 in price on XAUUSD. `<=`, so the boundary itself is
        a stack -- the conservative side of a guard."""
        open_entries([4300.0])
        boundary = 4300.0 + 20.0 * PIPS_TO_PRICE_XAUUSD

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, boundary) is True

    def test_it_measures_distance_not_direction(self, open_entries):
        open_entries([4300.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4299.0) is True

    def test_one_near_trade_among_several_far_ones_still_blocks(self, open_entries):
        open_entries([4200.0, 4400.0, 4301.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4300.0) is True

    def test_several_far_trades_do_not_block(self, open_entries):
        open_entries([4200.0, 4400.0, 4500.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4300.0) is False


class TestATradeThatHasNoPriceYet:
    def test_an_unfilled_placeholder_blocks(self, open_entries):
        """A row with entry 0 is an open template trade whose fill has not come
        back yet. It has no price to compare, so it counts as a stack: an
        invisible position is the one you least want to double up on, and
        bugs/016 is what an entry-0 row looks like when it goes wrong."""
        open_entries([0.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4300.0) is True

    def test_a_negative_entry_blocks_too(self, open_entries):
        """`<= 0`, not `== 0`. Nothing should ever write a negative entry; if
        something does, the guard fails closed rather than treating it as a
        price 4,300 points away."""
        open_entries([-1.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4300.0) is True

    def test_a_placeholder_blocks_even_beside_a_far_away_real_trade(self, open_entries):
        open_entries([9999.0, 0.0])

        assert sr._sig_guard_blocks("Chan", "BUY", 20.0, 4300.0) is True
