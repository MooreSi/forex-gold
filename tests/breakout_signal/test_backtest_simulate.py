"""The breakout engine's walk-forward simulator.

This is the counterfactual check. Its own docstring says why it exists: the
previous tuning loop -- nightly AI batch analysis adjusting params from small
recent samples -- "ratcheted the engine into a losing configuration with no
counterfactual check". So this harness is what stands between a recalibration
and live trading, and it was at 0% coverage.

What is pinned here is the arithmetic a tuning decision rests on:

  * every exit pays the round-trip cost, once, on the fraction it closes;
  * the scale-out ladder banks thirds and moves the stop the way the live
    engine does (BE+cost after TP1, TP1 after TP2);
  * a stop is checked BEFORE targets within the same bar -- the deliberate
    conservative tie-break, and the difference between an honest backtest and
    a flattering one;
  * running out of candles marks to the last close, rather than pretending the
    trade closed at entry (the mistake found in the other backtest, bugs/017 --
    this one already had it right).

No live path, no database, no broker. Synthetic M1 candles throughout.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import backtest as bt


PARAMS = {
    "tp1_mult": 1.0,
    "tp2_mult": 2.0,
    "tp3_mult": 3.0,
    "time_stop_mins": 60,
}


def _get(key):
    return PARAMS[key]


def _bars(specs, start_ts=1_000_000.0, step=60.0):
    """specs: list of (high, low) or (high, low, close)."""
    out = []
    for n, spec in enumerate(specs):
        hi, lo = spec[0], spec[1]
        close = spec[2] if len(spec) > 2 else (hi + lo) / 2
        out.append({"time": start_ts + n * step, "open": close,
                    "high": hi, "low": lo, "close": close})
    return out


def _run(direction, entry, sl_dist, bars, t0=1_000_000.0):
    times = [c["time"] for c in bars]
    return bt._simulate(direction, entry, sl_dist, t0, bars, times, get=_get)


class TestStops:
    def test_a_buy_stopped_out_loses_the_stop_plus_costs(self):
        pts, _ = _run("BUY", 4000.0, 10.0, _bars([(4000.5, 3990.0)]))
        assert pts == pytest.approx(-10.0 - bt.COST_PTS)

    def test_a_sell_stopped_out_loses_the_stop_plus_costs(self):
        """The mirror: a sell's stop is ABOVE entry and triggers on the high.
        Checking the low instead would never stop a sell out at all."""
        pts, _ = _run("SELL", 4000.0, 10.0, _bars([(4010.0, 3999.5)]))
        assert pts == pytest.approx(-10.0 - bt.COST_PTS)

    def test_THE_STOP_WINS_when_one_bar_touches_both(self):
        """The conservative intrabar tie-break the module docstring promises.
        A bar wide enough to hit both is ambiguous on M1; assuming the target
        filled first is exactly how a backtest flatters itself."""
        pts, _ = _run("BUY", 4000.0, 10.0, _bars([(4011.0, 3989.0)]))
        assert pts == pytest.approx(-10.0 - bt.COST_PTS), "the target was taken first"


class TestTheScaleOutLadder:
    def test_tp1_banks_a_third_and_leaves_the_rest_running(self):
        bars = _bars([(4010.5, 4000.0), (4001.0, 4000.5)])
        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        # a third banked at +10, remainder marked out at the last close
        assert pts > 0
        assert pts < 10.0, "the whole position was closed at TP1"

    def test_after_tp1_the_stop_moves_to_break_even_plus_cost(self):
        """Not to entry. Moving to entry exactly would book a small loss on
        the remainder every time price came back, which is not what the live
        engine does."""
        bars = _bars([(4010.5, 4000.0), (4001.0, 3999.0)])
        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        banked = (10.0 - bt.COST_PTS) * 0.33
        rest = (bt.COST_PTS - bt.COST_PTS) * 0.67
        assert pts == pytest.approx(banked + rest, abs=1e-6)

    def test_after_tp2_the_stop_moves_up_to_tp1(self):
        """The runner is protected at the first target once the second is in."""
        # The last close is deliberately 4014, NOT 4010: if the stop had
        # stayed at break-even the runner would be marked out at 4014 instead
        # of stopping at TP1, and the two would be indistinguishable if the
        # close happened to equal TP1. Mutation found that hole.
        bars = _bars([(4020.5, 4000.0), (4015.0, 4009.0, 4014.0)])
        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        banked = (10.0 - bt.COST_PTS) * 0.33 + (20.0 - bt.COST_PTS) * 0.33
        rest = (10.0 - bt.COST_PTS) * 0.34          # stopped at TP1 = +10
        assert pts == pytest.approx(banked + rest, abs=1e-6)

    def test_tp3_closes_the_whole_remainder(self):
        bars = _bars([(4030.5, 4000.0)])
        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        expected = ((10.0 - bt.COST_PTS) * 0.33
                    + (20.0 - bt.COST_PTS) * 0.33
                    + (30.0 - bt.COST_PTS) * 0.34)
        assert pts == pytest.approx(expected, abs=1e-6)

    def test_all_three_targets_in_ONE_bar_still_bank_all_three(self):
        """A fast move through the whole ladder must not be scored as a single
        TP1 hit -- the while loop exists for this."""
        one_bar, _ = _run("BUY", 4000.0, 10.0, _bars([(4030.5, 4000.0)]))
        staged, _ = _run("BUY", 4000.0, 10.0,
                         _bars([(4010.5, 4000.0), (4020.5, 4010.0),
                                (4030.5, 4020.0)]))
        assert one_bar == pytest.approx(staged, abs=1e-6)


class TestTheTimeStop:
    def test_a_losing_trade_is_cut_after_the_time_stop(self):
        """Only when it is meaningfully offside -- more than 0.2 x SL."""
        bars = _bars([(4001.0, 3996.0, 3996.0)], start_ts=1_000_000.0)
        bars[0]["time"] = 1_000_000.0 + 61 * 60          # past the 60-min stop

        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        assert pts == pytest.approx(-4.0 - bt.COST_PTS)

    def test_a_trade_only_SLIGHTLY_offside_is_left_alone(self):
        """-1pt on a 10pt stop is inside the 0.2 x SL band. Cutting there
        would turn normal noise into a realised loss."""
        # A later bar recovers to +3. If the time stop fired on the -1 bar
        # the result would be -1.35; because it does not, the trade is still
        # open at the end and marks out at +3. Without that second bar the two
        # outcomes are the same number and the test proves nothing -- which is
        # what mutation showed.
        bars = _bars([(4001.0, 3999.0, 3999.0), (4004.0, 4002.0, 4003.0)])
        bars[0]["time"] = 1_000_000.0 + 61 * 60
        bars[1]["time"] = 1_000_000.0 + 62 * 60

        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        assert pts == pytest.approx(3.0 - bt.COST_PTS), "the time stop cut too eagerly"

    def test_the_time_stop_does_not_apply_once_a_target_has_banked(self):
        """`remaining >= 0.999` gates it. A trade that has already taken TP1
        is a runner and is managed by its stop, not by the clock."""
        bars = _bars([(4010.5, 4000.0), (4001.0, 3996.0, 3996.0)])
        bars[1]["time"] = 1_000_000.0 + 61 * 60

        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        # Exact, not merely positive: cutting the runner here also leaves a
        # positive total (the banked third covers it), so "> 0" passed with
        # the gate removed. The runner must be stopped at break-even+cost,
        # not cut at -4.
        banked = (10.0 - bt.COST_PTS) * 0.33
        rest = (bt.COST_PTS - bt.COST_PTS) * 0.67
        assert pts == pytest.approx(banked + rest, abs=1e-6), (
            "the time stop cut a trade that had already banked TP1")


class TestRunningOutOfCandles:
    def test_it_marks_to_the_LAST_CLOSE_not_the_entry(self):
        """bugs/017 was this mistake in the other backtest. This harness
        already had it right, and this test is what keeps it that way."""
        bars = _bars([(4001.0, 3999.0, 3995.0)])

        pts, _ = _run("BUY", 4000.0, 10.0, bars)

        assert pts == pytest.approx(-5.0 - bt.COST_PTS)

    def test_a_winning_open_trade_is_marked_out_too(self):
        bars = _bars([(4006.0, 3999.0, 4005.0)])
        pts, _ = _run("BUY", 4000.0, 10.0, bars)
        assert pts == pytest.approx(5.0 - bt.COST_PTS)

    def test_no_candles_at_all_is_flat_not_a_crash(self):
        """Called per signal over a sliced history; an empty slice must not
        take the whole walk-forward run down."""
        pts, ts = bt._simulate("BUY", 4000.0, 10.0, 1_000_000.0, [], [], get=_get)
        assert pts == pytest.approx(-bt.COST_PTS)
        assert ts == 1_000_000.0


class TestCostsAreChargedOnce:
    def test_the_round_trip_cost_is_a_REAL_number(self):
        """Written as a literal on purpose. Every other assertion in this file
        spells the cost as bt.COST_PTS, so setting that constant to 0 moves
        both sides and they all still pass -- mutation caught exactly that.
        One test has to know the actual figure."""
        assert bt.COST_PTS == 0.35

        pts, _ = _run("BUY", 4000.0, 10.0, _bars([(4000.5, 3990.0)]))

        assert pts == pytest.approx(-10.35), "the spread/fee cost was not charged"

    def test_each_scaled_leg_pays_its_own_share(self):
        """Cost is charged per leg on the fraction closed, so three legs pay
        one round trip in total, not three."""
        pts, _ = _run("BUY", 4000.0, 10.0, _bars([(4030.5, 4000.0)]))
        gross = 10.0 * 0.33 + 20.0 * 0.33 + 30.0 * 0.34
        assert pts == pytest.approx(gross - bt.COST_PTS, abs=1e-6)


class TestSessionLabelling:
    @pytest.mark.parametrize("hour,expected", [
        (0, "asian"), (7, "asian"), (23, "asian"),
        (8, "london"), (11, "london"),
        (12, "overlap"), (16, "overlap"),
        (17, "ny"), (20, "ny"),
        (21, "off"), (22, "off"),
    ])
    def test_each_hour_maps_to_its_session(self, hour, expected):
        """Results are broken down per session, so a mislabelled hour moves
        trades into the wrong bucket and skews which session looks profitable."""
        import datetime as _dt
        ts = _dt.datetime(2026, 8, 26, hour, 30, tzinfo=_dt.timezone.utc).timestamp()
        assert bt._session_at(ts) == expected

    def test_saturday_is_closed(self):
        import datetime as _dt
        ts = _dt.datetime(2026, 8, 29, 12, 0, tzinfo=_dt.timezone.utc).timestamp()
        assert bt._session_at(ts) == "closed"

    def test_sunday_reopens_at_22_00(self):
        """The week opens mid-Sunday. Treating all of Sunday as closed would
        silently drop the Asian open."""
        import datetime as _dt
        before = _dt.datetime(2026, 8, 30, 21, 0, tzinfo=_dt.timezone.utc).timestamp()
        after = _dt.datetime(2026, 8, 30, 23, 0, tzinfo=_dt.timezone.utc).timestamp()
        assert bt._session_at(before) == "closed"
        assert bt._session_at(after) == "asian"
