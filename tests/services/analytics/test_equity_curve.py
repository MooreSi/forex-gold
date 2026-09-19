"""The Analysis tab's equity curve.

Asked for on 2026-09-19 ("missing... the equity curve").

Built from the SAME rows the deal-level trade table already fetched, not from
a second read: the curve and the table must agree, and two independent reads
of a moving broker are two chances to disagree on screen.

**It is realised P&L over the window, and it says so.** Calling it "equity"
and starting it at the account balance would require inventing where the
account stood when the window opened -- that figure is not in the deal
history, and a curve whose zero is a guess is a curve that can be read as a
loss when the account never moved. The header's own P&L figure answers "where
is the account overall"; this answers "what did the trading do this month".
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import equity_curve


def _row(close_ts, pnl, ticket=1):
    return {"ticket": ticket, "close_ts": close_ts, "pnl": pnl}


class TestThePoints:

    def test_it_starts_at_zero_before_the_first_trade(self):
        # A curve that starts at the first trade's P&L has no baseline to be
        # read against.
        curve = equity_curve.build([_row(100, 50.0)])

        assert curve["points"][0]["pnl"] == 0.0

    def test_each_trade_adds_a_point(self):
        curve = equity_curve.build([_row(100, 50.0, 1), _row(200, -20.0, 2)])

        assert len(curve["points"]) == 3

    def test_the_running_total_accumulates(self):
        curve = equity_curve.build([_row(100, 50.0, 1), _row(200, -20.0, 2),
                                    _row(300, 10.0, 3)])

        assert [p["pnl"] for p in curve["points"]] == [0.0, 50.0, 30.0, 40.0]

    def test_it_walks_forward_in_time_whatever_order_it_was_given(self):
        # The table sorts newest-first. A curve drawn in that order runs
        # backwards, which looks like a mirror image of the month.
        curve = equity_curve.build([_row(300, 10.0, 3), _row(100, 50.0, 1),
                                    _row(200, -20.0, 2)])

        stamps = [p["ts"] for p in curve["points"][1:]]
        assert stamps == sorted(stamps)

    def test_every_point_carries_its_moment(self):
        curve = equity_curve.build([_row(1757955600, 50.0)])

        assert curve["points"][-1]["ts"] == 1757955600


class TestWhatItMeasures:

    def test_the_net_is_the_last_point(self):
        curve = equity_curve.build([_row(100, 50.0, 1), _row(200, -20.0, 2)])

        assert curve["net"] == pytest.approx(30.0)

    def test_the_peak_is_the_best_the_curve_ever_was(self):
        curve = equity_curve.build([_row(100, 80.0, 1), _row(200, -50.0, 2)])

        assert curve["peak"] == pytest.approx(80.0)

    def test_the_drawdown_is_measured_from_the_peak_not_from_zero(self):
        # The distinction that matters. Up 80 then down to 30 is a 50
        # drawdown, not a 30 profit with nothing wrong.
        curve = equity_curve.build([_row(100, 80.0, 1), _row(200, -50.0, 2)])

        assert curve["max_drawdown"] == pytest.approx(50.0)

    def test_a_curve_that_only_rises_has_no_drawdown(self):
        curve = equity_curve.build([_row(100, 10.0, 1), _row(200, 10.0, 2)])

        assert curve["max_drawdown"] == 0.0

    def test_a_curve_that_never_recovers_still_reports_its_worst(self):
        curve = equity_curve.build([_row(100, -10.0, 1), _row(200, -30.0, 2)])

        assert curve["max_drawdown"] == pytest.approx(40.0)
        assert curve["net"] == pytest.approx(-40.0)

    def test_it_counts_the_trades_it_drew(self):
        curve = equity_curve.build([_row(100, 1.0, 1), _row(200, 2.0, 2)])

        assert curve["trades"] == 2


class TestTheEdges:

    def test_no_trades_is_an_empty_curve_not_a_flat_line_at_zero(self):
        # A flat line reads as "traded all month and broke even".
        curve = equity_curve.build([])

        assert curve["points"] == []
        assert curve["trades"] == 0

    def test_a_row_with_no_close_time_is_left_out_rather_than_placed_at_1970(self):
        curve = equity_curve.build([_row(0, 50.0, 1), _row(200, 10.0, 2)])

        assert curve["trades"] == 1
        assert curve["net"] == pytest.approx(10.0)

    def test_a_row_with_a_non_numeric_pnl_does_not_break_the_curve(self):
        curve = equity_curve.build([
            {"ticket": 1, "close_ts": 100, "pnl": None},
            _row(200, 10.0, 2),
        ])

        assert curve["net"] == pytest.approx(10.0)

    def test_it_does_not_care_what_else_is_on_the_row(self):
        # It is handed the trade table's rows whole, and those carry twenty
        # fields it has no business knowing about.
        curve = equity_curve.build([
            {"ticket": 1, "close_ts": 100, "pnl": 5.0, "strategy": "Breakout",
             "max_tp": "TP2", "group": None},
        ])

        assert curve["net"] == pytest.approx(5.0)
