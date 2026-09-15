"""The split summary must never render a thin side as zeros -- docs/todo/003.

The same failure the "NOT SIMULATED" badge exists to prevent, one table
lower. `tests/backtest/test_unsupported_template_reason.py` records it: a row
of zeros beside a row with real figures reads as a strategy that traded
nothing and lost nothing, which is an argument FOR the half that was never
measured. A split halves the trade count on each side, so it manufactures
exactly the thin sides that would produce those zeros.

These test the decision, which is pure. They do not test that the section
appears on screen: the split table is only built after a backtest run, and a
run needs candles from the MT5 bridge, which no test may touch. What IS
covered on screen is the page itself -- `tests/frontend/test_remaining_pages_render.py`
carries two validated landmarks for it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.backtest.engine import StrategyStats
from frontend.pages.backtest import _split


def _side(**over) -> StrategyStats:
    base = dict(strategy="conservative", trades=24, wins=18, losses=6,
                win_rate=0.75, total_pnl=1234.5, total_commission=42.0,
                avg_win=100.0, avg_loss=-50.0, profit_factor=3.0,
                max_drawdown_pct=7.25, sharpe=1.1, final_balance=11234.5)
    base.update(over)
    return StrategyStats(**base)


class TestAThinSideSaysSoRatherThanShowingZeros:

    def test_a_side_with_no_stats_returns_its_note(self):
        note = "6 trades -- too few to measure (min 20, provisional; see docs/simon-handover/036)"

        assert _split.side_row(None, note) == note

    def test_it_is_not_a_row_of_cells(self):
        """The assertion that actually matters.

        Returning a note is only useful if it cannot be mistaken for cell
        data. A `["0", "0.0%", "$0.00", ...]` fallback would satisfy "renders
        something" and reintroduce the exact table this guards against.
        """
        got = _split.side_row(None, "6 trades -- too few to measure")

        assert not isinstance(got, list)

    def test_a_measured_side_returns_five_cells(self):
        """Capability control: without this the test above passes against an
        implementation that returns a note for every side, measured or not."""
        got = _split.side_row(_side(), "")

        assert isinstance(got, list) and len(got) == len(_split.COLUMNS)

    def test_a_measured_side_carries_its_own_numbers(self):
        got = _split.side_row(_side(trades=24, win_rate=0.75, total_pnl=1234.5), "")

        assert got[0] == "24"
        assert "75" in got[1]
        assert "1,234.50" in got[2]


class TestProfitFactor:

    def test_infinite_renders_as_a_symbol(self):
        """`float('inf')` formats as "inf" through every default path. The
        comparison table above already renders it as a symbol; two tables on
        one screen disagreeing about the same quantity is its own bug."""
        got = _split.side_row(_side(profit_factor=float("inf")), "")

        assert got[3] == "∞"

    def test_a_finite_one_is_a_number(self):
        got = _split.side_row(_side(profit_factor=3.0), "")

        assert got[3] == "3.00"


class TestTheBoundaryIsLabelledInUTC:
    """A signal's created_ts is true UTC. `_BROKER_TZ_OFFSET` exists because
    MT5 candle timestamps are UTC+3, and applying it here would slide the
    stated cut three hours from where it actually fell."""

    def test_it_matches_the_format_the_page_already_uses(self):
        ts = 1_700_000_000.0
        expected = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")

        assert _split.utc_label(ts) == expected

    def test_no_broker_offset_is_applied(self):
        """Negative control: three hours out is what a copied offset looks
        like, and it would be invisible without asserting the hour."""
        from backend.src.services.backtest.engine import _BROKER_TZ_OFFSET

        ts = 1_700_000_000.0
        shifted = _split.utc_label(ts + _BROKER_TZ_OFFSET)

        assert _split.utc_label(ts) != shifted
        assert _split.utc_label(ts).endswith("22:13 UTC")
