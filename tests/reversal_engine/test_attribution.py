"""Post-trade attribution: which cohorts make money and which do not.

Section 5.8 of docs/todo/reversal-engine/200. Most of the analysis in
data-inspect/003 was hand-written SQL on a Monday morning. A desk has it
standing, because it is the difference between managing a strategy and
periodically rediscovering it.

The axes are chosen from what the measurements already found mattered:
level type, session, whether breakeven moved (item 030's suspect), and how
long the fill took (item 040's cohort).
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import attribution as at


T0 = 1_788_739_200.0


def row(outcome="win", pnl_pts=3.0, sl_dist=5.0, level_type="round_5",
        session="london", be=0, created_at=T0, trigger_time=T0 + 600.0,
        net=10.0):
    return {"outcome": outcome, "pnl_pts": pnl_pts, "sl_dist": sl_dist,
            "level_type": level_type, "session": session,
            "sl_moved_to_be": be, "created_at": created_at,
            "trigger_time": trigger_time, "net_pnl_dollars": net}


class TestTheNumbers:
    def test_r_is_the_points_made_over_the_stop_that_defined_it(self):
        out = at.cohorts([row(pnl_pts=2.5, sl_dist=5.0)])
        assert out["level_type"]["round_5"].mean_r == pytest.approx(0.5)

    def test_win_rate_and_net_come_out_per_cohort(self):
        rows = [row(level_type="round_5", outcome="win", net=10.0),
                row(level_type="round_5", outcome="loss", net=-20.0),
                row(level_type="asia_low", outcome="win", net=5.0)]
        out = at.cohorts(rows)["level_type"]
        assert out["round_5"].n == 2
        assert out["round_5"].win_rate == pytest.approx(0.5)
        assert out["round_5"].net == pytest.approx(-10.0)
        assert out["asia_low"].n == 1

    def test_a_row_with_no_stop_distance_is_excluded_from_r_not_counted_as_zero(self):
        """A zero stop makes R undefined, not zero. Averaging a fabricated
        0.0 in drags every cohort toward the middle and hides the thing the
        table exists to show."""
        out = at.cohorts([row(sl_dist=0.0, pnl_pts=3.0), row(sl_dist=5.0, pnl_pts=5.0)])
        c = out["level_type"]["round_5"]
        assert c.mean_r == pytest.approx(1.0)
        assert c.n == 2
        assert c.n_with_r == 1


class TestTheAxes:
    def test_breakeven_is_an_axis_because_item_030_says_it_matters(self):
        rows = [row(be=1, pnl_pts=1.0), row(be=0, pnl_pts=4.0)]
        out = at.cohorts(rows)["breakeven"]
        assert out["moved"].mean_r == pytest.approx(0.2)
        assert out["not moved"].mean_r == pytest.approx(0.8)

    def test_fill_delay_is_bucketed_because_item_040_says_it_matters(self):
        fast = row(created_at=T0, trigger_time=T0 + 60.0)
        slow = row(created_at=T0, trigger_time=T0 + 4000.0)
        out = at.cohorts([fast, slow])["fill_delay"]
        assert set(out) == {"under 5m", "over 1h"}

    def test_session_is_an_axis(self):
        out = at.cohorts([row(session="asian"), row(session="london")])
        assert set(out["session"]) == {"asian", "london"}

    def test_a_caller_can_add_an_axis_without_this_module_knowing_about_it(self):
        """Regime is the obvious one, and this module must not grow a fourth
        implementation of regime classification -- there are already three
        in the codebase."""
        out = at.cohorts([row(), row()],
                         extra_axes={"regime": lambda r: "trending"})
        assert out["regime"]["trending"].n == 2


class TestReadability:
    def test_it_renders_a_table_a_human_can_read_in_a_log(self):
        text = at.render(at.cohorts([row(), row(outcome="loss", net=-5.0)]))
        assert "level_type" in text
        assert "round_5" in text

    def test_an_empty_sample_renders_a_statement_not_an_empty_string(self):
        assert "no closed" in at.render(at.cohorts([])).lower()
