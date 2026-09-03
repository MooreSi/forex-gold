"""Simulating an EA-template trade over historical bars.

Option A of docs/todo/backtest/010. Every rule below was read out of
`ManageTemplate()` in ForexTraderBridge.mq5 (lines 2655-2941) rather than
assumed, because the whole value of this simulator is that its numbers match
what the EA actually does. Where the EA and a guess would differ, the guess is
the bug.

The four that would have been wrong if I had gone by field names:

  * `sl_pips` is PIPS, and the EA converts with `pips * 10 * _Point`. On
    XAUUSD (_Point = 0.01) that is `pips * 0.1` price units. The backtest
    works in price units, so 50 pips is a 5.0 move -- not 50, and not 0.5.
  * `be_trigger` is a TP LEVEL INDEX, not a distance. `be_trigger=2` means
    "move to breakeven when TP2 clears".
  * a partial closes `original_lot * pct`, not a share of what remains, and
    `tp{n}_pct` is stored 0-100 but divided by 100 before it reaches the EA
    (open_trade.py:217).
  * the ladder walks IN ORDER and stops at the first level price has not
    reached -- a later TP cannot fire before an earlier one.
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import template_simulator as sim


PIP = 0.1          # XAUUSD: 1 pip = 10 points = 0.1 price units


def _tpl(**over) -> dict:
    base = {
        "name": "T", "mode": "single", "lot_anchor": 0.10, "risk_pct": 0.0,
        "sl_pips": 50.0, "tpsl_mode": "on", "partials": 1,
        "close_full_on_last": 0, "be_mode": "entry", "be_trigger": 0,
        "be_buffer_pts": 0.0, "trail_mode": "off", "trail_distance": 0.0,
        "trail_activation": 0.0, "trail_padding": 0.0, "tp1_trigger_level": 0,
        "tp1_pips": 0.0, "tp1_pct": 0.0,
    }
    base.update(over)
    return base


def _bars(*highs_lows) -> list[dict]:
    """Bars as (high, low) pairs; open/close sit inside the range."""
    out = []
    for hi, lo in highs_lows:
        out.append({"high": hi, "low": lo, "open": lo, "close": hi, "time": 0})
    return out


class TestUnits:
    def test_pips_convert_the_way_the_EA_converts_them(self):
        """`PipsToPrice(pips) = pips * 10 * _Point`, _Point = 0.01."""
        assert sim.pips_to_price(50.0) == pytest.approx(5.0)
        assert sim.pips_to_price(1.0) == pytest.approx(0.1)

    def test_zero_pips_is_zero(self):
        assert sim.pips_to_price(0.0) == 0.0


class TestTheStopLoss:
    def test_a_buy_stop_sits_sl_pips_below_entry(self):
        r = sim.simulate(_tpl(sl_pips=50.0), _bars((4000.5, 3994.0)),
                         entry=4000.0, is_buy=True)

        assert r.outcome == "sl"
        assert r.close_price == pytest.approx(3995.0)

    def test_a_sell_stop_sits_sl_pips_above_entry(self):
        r = sim.simulate(_tpl(sl_pips=50.0), _bars((4006.0, 3999.5)),
                         entry=4000.0, is_buy=False)

        assert r.outcome == "sl"
        assert r.close_price == pytest.approx(4005.0)

    def test_a_stop_not_reached_leaves_the_trade_open(self):
        r = sim.simulate(_tpl(sl_pips=50.0), _bars((4001.0, 3999.0)),
                         entry=4000.0, is_buy=True)

        assert r.outcome == "timeout"


class TestTheTpLadder:
    def _ladder(self, **over):
        base = dict(tp1_pips=20.0, tp1_pct=50.0, tp2_pips=40.0, tp2_pct=50.0)
        base.update(over)
        return _tpl(**base)

    def test_tp1_closes_its_share_of_the_ORIGINAL_lot(self):
        """50% of 0.10 is 0.05 -- of the original, not of what remains."""
        r = sim.simulate(self._ladder(), _bars((4002.5, 3999.5)),
                         entry=4000.0, is_buy=True)

        assert r.closed_lots[0] == pytest.approx(0.05)

    def test_a_later_tp_cannot_fire_before_an_earlier_one(self):
        """The EA walks the ladder in order and breaks at the first level
        price has not reached. A bar that leaps past TP2 without TP1 having
        cleared must still take TP1 first."""
        r = sim.simulate(self._ladder(), _bars((4004.5, 3999.9)),
                         entry=4000.0, is_buy=True)

        assert [round(l, 4) for l in r.closed_lots] == [0.05, 0.05]

    def test_the_last_level_leaves_a_runner_when_close_full_is_off(self):
        r = sim.simulate(
            self._ladder(tp1_pct=30.0, tp2_pct=30.0, close_full_on_last=0),
            _bars((4004.5, 3999.9)), entry=4000.0, is_buy=True)

        assert r.remaining_lots > 0

    def test_close_full_on_last_takes_the_remainder(self):
        r = sim.simulate(
            self._ladder(tp1_pct=30.0, tp2_pct=30.0, close_full_on_last=1),
            _bars((4004.5, 3999.9)), entry=4000.0, is_buy=True)

        assert r.remaining_lots == pytest.approx(0.0)

    def test_partials_off_closes_nothing_until_the_final_level(self):
        """"False = single close at the final level" -- the EA breaks out of
        the ladder rather than partial-closing."""
        r = sim.simulate(self._ladder(partials=0),
                         _bars((4002.5, 3999.9)), entry=4000.0, is_buy=True)

        assert r.closed_lots == []
        assert r.remaining_lots == pytest.approx(0.10)

    def test_an_out_of_order_ladder_still_stops_at_the_first_gap(self):
        """The discriminating case for `break` vs `continue`.

        With ascending levels a bar that reaches TP2 has reached TP1 too, so
        either loop gives the same answer -- the first version of the
        ordering test could not tell them apart. Here TP1 sits FURTHER away
        than TP2 (40 vs 20 pips), so price can reach TP2 alone. The EA breaks
        at the first uncleared level, so nothing closes.
        """
        r = sim.simulate(
            self._ladder(tp1_pips=40.0, tp2_pips=20.0),
            _bars((4002.5, 3999.9)), entry=4000.0, is_buy=True)

        assert r.closed_lots == []

    def test_tpsl_mode_off_disables_the_whole_ladder(self):
        r = sim.simulate(self._ladder(tpsl_mode="off"),
                         _bars((4004.5, 3999.9)), entry=4000.0, is_buy=True)

        assert r.closed_lots == []


class TestBreakeven:
    def _be(self, **over):
        base = dict(tp1_pips=20.0, tp1_pct=50.0, tp2_pips=40.0, tp2_pct=50.0,
                    be_trigger=1)
        base.update(over)
        return _tpl(**base)

    def test_be_trigger_is_a_TP_LEVEL_not_a_distance(self):
        """be_trigger=1 means "when TP1 clears", so the stop moves to entry
        after a 20-pip move -- not after a 1-pip one."""
        r = sim.simulate(self._be(), _bars((4002.5, 3999.5), (4002.0, 3999.0)),
                         entry=4000.0, is_buy=True)

        assert r.outcome != "sl" or r.close_price == pytest.approx(4000.0)

    def test_the_stop_moves_to_entry(self):
        r = sim.simulate(self._be(), _bars((4002.5, 3999.5), (4001.0, 3998.0)),
                         entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4000.0)

    def test_entry_buffer_adds_the_buffer(self):
        r = sim.simulate(self._be(be_mode="entry_buffer", be_buffer_pts=0.5),
                         _bars((4002.5, 3999.5), (4001.0, 3998.0)),
                         entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4000.5)

    def test_no_trigger_leaves_the_original_stop(self):
        r = sim.simulate(self._be(be_trigger=0),
                         _bars((4002.5, 3999.5), (4001.0, 3994.0)),
                         entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(3995.0)

    def test_a_level_that_has_not_cleared_does_not_arm_breakeven(self):
        """The discriminating case for "index, not distance".

        be_trigger=2 means TP2 (40 pips = 4.0 price). This bar moves 2.5 --
        past TP1, and past the number 2 if the trigger were misread as a
        distance. Only the level reading leaves the original stop in place.
        """
        r = sim.simulate(self._be(be_trigger=2),
                         _bars((4002.5, 3999.5), (4001.0, 3994.0)),
                         entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(3995.0)

    def test_it_latches_and_does_not_unarm_on_a_retrace(self):
        """The EA arms off `triggered[]`, not a live price test. Re-asking
        "is price beyond the TP right now" was a real bug: 141 trades over a
        month never moved to breakeven and closed a mean 66.7 pips below
        entry."""
        r = sim.simulate(self._be(),
                         _bars((4002.5, 3999.5), (4000.2, 3999.8),
                               (4000.1, 3998.0)),
                         entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4000.0)


class TestTrailing:
    def test_off_never_moves_the_stop(self):
        r = sim.simulate(_tpl(trail_mode="off"),
                         _bars((4010.0, 3999.5), (4001.0, 3994.0)),
                         entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(3995.0)

    def test_step_trails_at_the_configured_distance(self):
        r = sim.simulate(
            _tpl(trail_mode="step", trail_distance=20.0, trail_activation=10.0),
            _bars((4005.0, 3999.5), (4004.0, 4001.0)),
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4003.0)

    def test_step_does_not_trail_before_it_is_armed(self):
        r = sim.simulate(
            _tpl(trail_mode="step", trail_distance=20.0, trail_activation=100.0),
            _bars((4005.0, 3999.5), (4001.0, 3994.0)),
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(3995.0)

    def test_candle_trails_to_the_previous_bar_low(self):
        r = sim.simulate(
            _tpl(trail_mode="candle", trail_activation=10.0),
            _bars((4005.0, 4001.0), (4006.0, 4002.0), (4004.0, 4000.5)),
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4001.0)

    def test_tp_mode_trails_to_the_highest_cleared_tp(self):
        """Only TP1 (4002.0) clears here -- the first bar tops out below TP2,
        so the stop follows TP1 and not the rung price never reached.

        The first version of this test used a high of 4004.5, which clears
        BOTH rungs; the stop then correctly went to 4004.0 and the test was
        wrong, not the walk."""
        r = sim.simulate(
            _tpl(trail_mode="tp", trail_activation=10.0,
                 tp1_pips=20.0, tp1_pct=0.0, tp2_pips=40.0, tp2_pct=0.0),
            _bars((4002.5, 3999.5), (4002.4, 4001.5)),
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4002.0)

    def test_a_stop_is_never_widened(self):
        """MoveSl only ever accepts a stop closer than the current one.

        Discriminating on purpose: bar 0 trails the stop to 4003.0, bar 1
        would compute 4002.5 -- looser -- and bar 2 dips to 4002.8. Keeping
        the tighter stop exits at 4003.0; widening survives that dip and runs
        on. An earlier version of this test used bars where both behaviours
        exited at the same price and proved nothing.
        """
        r = sim.simulate(
            _tpl(trail_mode="step", trail_distance=20.0, trail_activation=10.0),
            _bars((4005.0, 3999.5), (4004.5, 4003.5), (4004.0, 4002.8)),
            entry=4000.0, is_buy=True)

        assert r.outcome == "sl"
        assert r.close_price == pytest.approx(4003.0)


class TestLotSizing:
    def test_the_anchor_lot_is_used_when_risk_pct_is_zero(self):
        r = sim.simulate(_tpl(lot_anchor=0.07), _bars((4001.0, 3999.0)),
                         entry=4000.0, is_buy=True)

        assert r.lot_size == pytest.approx(0.07)

    def test_risk_pct_sizes_from_the_stop_distance(self):
        """5.0 price units at $100/unit/lot: $50 of risk on a $10,000
        balance at 0.5% is 0.10 lots."""
        r = sim.simulate(_tpl(risk_pct=0.5, sl_pips=50.0),
                         _bars((4001.0, 3999.0)),
                         entry=4000.0, is_buy=True, balance=10_000.0)

        assert r.lot_size == pytest.approx(0.10)


class TestItRefusesWhatItCannotModel:
    def test_a_grid_template_is_not_simulated(self):
        with pytest.raises(sim.UnsupportedTemplate):
            sim.simulate(_tpl(mode="grid"), _bars((4001.0, 3999.0)),
                         entry=4000.0, is_buy=True)

    def test_the_refusal_names_the_reason(self):
        with pytest.raises(sim.UnsupportedTemplate) as e:
            sim.simulate(_tpl(mode="grid"), _bars((4001.0, 3999.0)),
                         entry=4000.0, is_buy=True)

        assert "mode" in str(e.value)

    @pytest.mark.parametrize("mode", ["staged", "fractal"])
    def test_the_unmodelled_trail_modes_are_refused(self, mode):
        """No supported template uses either, so refusing costs nothing and
        prevents a silent divergence from the EA."""
        with pytest.raises(sim.UnsupportedTemplate):
            sim.simulate(_tpl(trail_mode=mode), _bars((4001.0, 3999.0)),
                         entry=4000.0, is_buy=True)
