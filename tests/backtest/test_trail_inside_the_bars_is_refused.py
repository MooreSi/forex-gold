"""A trail tighter than the bars cannot be walked on those bars.

`simulate` tests the stop using the level it held at the END of the previous
bar, then advances the trail from THIS bar's high. Inside one bar the trail
can move and never be tested; the live EA trails and tests on every tick.

The gap is invisible while the trail is wide and fatal once it is narrow, and
it does not announce itself — it shows up only as an edge that GROWS the
coarser the bars get. Measured 2026-09-22 on 261 real fills, one template,
identical entries, only the bar size changing:

    M1  PF 1.22 (+0.26)   M5  1.74 (+1.24)
    M15 1.92 (+2.27)      H1  1.91 (+4.80)

A real edge does not depend on the resolution it is measured at. That template
carried a $1.50 trail against median bar ranges of $1.73 (M1) through $16.68
(H1) — unsimulatable on every series available, including the finest. It was
recommended off the M5 number and lost money live.

**The check is on the PAIRING, not the template.** It lives beside the walk
that has the candles, not in `can_simulate`, because the same template is
honest at M1 and meaningless at H1. It is deliberately not enforced inside
`simulate` either: the trail mechanics are unit tested on coarse synthetic
bars where the arithmetic is readable, and those tests are right.
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import engine as bt
from backend.src.services.backtest import template_simulator as ts


def _bars(n: int, rng: float, start: float = 4000.0) -> list[dict]:
    """`n` rising bars each spanning exactly `rng`."""
    return [{"ts": 20_000 + i * 60, "open": start + i * 0.10,
             "low": start + i * 0.10, "high": start + i * 0.10 + rng,
             "close": start + i * 0.10 + rng * 0.5} for i in range(n)]


def _tpl(**over) -> dict:
    base = {
        "name": "T", "mode": "single", "pendings": 0, "lot_anchor": 0.10,
        "risk_pct": 0.0, "sl_pips": 200.0, "tpsl_mode": "on", "partials": 1,
        "close_full_on_last": 1, "be_mode": "entry", "be_trigger": 0,
        "tp1_pips": 500.0, "tp1_pct": 100.0,
        "trail_mode": "step", "trail_activation": 20.0,
        "trail_distance": 15.0, "trail_step": 5.0,
    }
    base.update(over)
    return base


class TestTheReason:
    def test_a_trail_narrower_than_the_bars_is_refused(self):
        # 15 pips = $1.50 of trail, against bars spanning $4.00.
        reason = ts.trail_inside_bars_reason(_tpl(), _bars(40, 4.00))

        assert reason != ""
        assert "trail_distance" in reason

    def test_the_reason_names_both_numbers_so_it_can_be_acted_on(self):
        reason = ts.trail_inside_bars_reason(_tpl(), _bars(40, 4.00))

        assert "1.50" in reason      # the trail distance
        assert "4.00" in reason      # the median bar range it sits inside

    def test_a_trail_wider_than_the_bars_is_allowed(self):
        # Negative control: a refusal that fires on everything protects nothing.
        reason = ts.trail_inside_bars_reason(
            _tpl(trail_distance=150.0), _bars(40, 2.00))

        assert reason == ""

    def test_a_trail_exactly_the_bar_range_is_allowed(self):
        # The boundary is stated rather than left to whichever way > rounds.
        reason = ts.trail_inside_bars_reason(
            _tpl(trail_distance=20.0), _bars(40, 2.00))

        assert reason == ""

    def test_no_trail_is_never_refused(self):
        assert ts.trail_inside_bars_reason(
            _tpl(trail_mode="off"), _bars(40, 4.00)) == ""

    def test_an_empty_series_is_not_refused_for_its_trail(self):
        # No bars means no median. The walk has other reasons to return
        # nothing; inventing a trail refusal here would mislabel them.
        assert ts.trail_inside_bars_reason(_tpl(), []) == ""


class TestRunBacktestSurfacesIt:
    """The refusal has to REACH the comparison table, or it protects nobody."""

    @pytest.fixture
    def narrow_trail_template(self, monkeypatch):
        monkeypatch.setattr(bt, "_load_backtest_template",
                            lambda name: _tpl() if name == "Narrow" else None)

    @pytest.fixture
    def wide_trail_template(self, monkeypatch):
        monkeypatch.setattr(
            bt, "_load_backtest_template",
            lambda name: _tpl(trail_distance=150.0) if name == "Wide" else None)

    def _signal(self):
        return bt.BtSignal(
            signal_id="s1", direction="BUY", entry_low=3999.5, entry_high=4000.5,
            stop_loss=3980.0, tp1=4050.0, tp2=None, tp3=None, created_ts=0.0)

    def test_a_refused_pairing_reports_a_sentence_not_zeros(
            self, narrow_trail_template):
        out = bt.run_backtest([self._signal()], _bars(40, 4.00),
                              ["template:Narrow"])

        stats = out["template:Narrow"]
        assert "trail_distance" in stats.unsupported_reason

    def test_a_refused_pairing_walks_nothing(self, narrow_trail_template):
        # Zeros WITH a reason are fine; zeros without one read as "safe".
        out = bt.run_backtest([self._signal()], _bars(40, 4.00),
                              ["template:Narrow"])

        assert out["template:Narrow"].trades == 0

    def test_a_workable_pairing_is_not_refused(self, wide_trail_template):
        out = bt.run_backtest([self._signal()], _bars(40, 2.00),
                              ["template:Wide"])

        assert out["template:Wide"].unsupported_reason == ""
