"""Simulating an EA-template trade over real ticks.

Phase 1 of docs/todo/backtest/010. Same rules as the bar walk
(test_template_simulator.py), but every comparison resolves against actual
bid/ask instead of a bar's high/low -- removing the same-bar "stop or target
first" ambiguity the bar walk resolves pessimistically, because a real tick
is only ever on one side of a level at a time.

Side discipline, read out of ManageTemplate() (mql5:2696-2941): a BUY marks
against `tick.bid`, a SELL against `tick.ask` -- TpCleared()'s own
`tick.bid >= val` / `tick.ask <= val` and the three `favMove`/`inProfit`
reads all agree. Getting this backwards would bias every BUY worse and every
SELL better without producing an obviously wrong number.
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import template_simulator as sim


PIP = 0.1


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


def _ticks(*bid_ask_pairs, spread=0.02) -> list[dict]:
    """Ticks from a list of bids; ask is bid+spread unless given as a pair."""
    out = []
    for i, item in enumerate(bid_ask_pairs):
        bid, ask = item if isinstance(item, tuple) else (item, item + spread)
        out.append({"time": i, "bid": bid, "ask": ask})
    return out


class TestSideDiscipline:
    def test_a_buy_stop_is_read_off_bid(self):
        """entry=4000, sl_pips=50 -> stop=3995. A tick whose ASK is still
        above 3995 but whose BID has dropped to it must still hit the stop
        -- an ask-based check would miss this and prove the wrong side is
        wired in."""
        r = sim.simulate_ticks(
            _tpl(sl_pips=50.0),
            _ticks((3995.0, 3995.5), (3994.0, 3994.5)),
            entry=4000.0, is_buy=True)

        assert r.outcome == "sl"
        assert r.close_price == pytest.approx(3995.0)

    def test_a_sell_stop_is_read_off_ask(self):
        """entry=4000, sl_pips=50 -> stop=4005 for a SELL. A tick whose BID
        is still below 4005 but whose ASK has risen to it must hit the stop
        -- a bid-based check would miss this."""
        r = sim.simulate_ticks(
            _tpl(sl_pips=50.0),
            _ticks((4004.5, 4005.0), (4004.0, 4006.0)),
            entry=4000.0, is_buy=False)

        assert r.outcome == "sl"
        assert r.close_price == pytest.approx(4005.0)

    def test_a_buy_tp_clears_off_bid(self):
        r = sim.simulate_ticks(
            _tpl(tp1_pips=20.0, tp1_pct=100.0),
            _ticks((4001.9, 4001.95), (4002.0, 4002.05)),
            entry=4000.0, is_buy=True)

        assert r.outcome == "tp"
        assert r.close_price == pytest.approx(4002.0)

    def test_a_sell_tp_clears_off_ask(self):
        r = sim.simulate_ticks(
            _tpl(tp1_pips=20.0, tp1_pct=100.0),
            _ticks((3997.95, 3998.1), (3997.9, 3998.0)),
            entry=4000.0, is_buy=False)

        assert r.outcome == "tp"
        assert r.close_price == pytest.approx(3998.0)


class TestNoSameTickAmbiguity:
    def test_stop_and_target_cannot_both_resolve_on_one_tick(self):
        """A single tick has exactly one bid and one ask -- there is no bar
        spanning both a stop and a target to be ambiguous about. This tick's
        bid clears the SL; the ladder must not also have fired."""
        r = sim.simulate_ticks(
            _tpl(sl_pips=10.0, tp1_pips=10.0, tp1_pct=100.0),
            _ticks((3999.0, 3999.05)),
            entry=4000.0, is_buy=True)

        assert r.outcome == "sl"
        assert r.closed_lots == []


class TestTheTpLadder:
    def test_tp1_closes_its_share_of_the_original_lot(self):
        r = sim.simulate_ticks(
            _tpl(tp1_pips=20.0, tp1_pct=50.0, tp2_pips=40.0, tp2_pct=50.0),
            _ticks(4002.5),
            entry=4000.0, is_buy=True)

        assert r.closed_lots[0] == pytest.approx(0.05)

    def test_a_later_tp_cannot_fire_before_an_earlier_one(self):
        r = sim.simulate_ticks(
            _tpl(tp1_pips=40.0, tp1_pct=50.0, tp2_pips=20.0, tp2_pct=50.0),
            _ticks(4002.5),
            entry=4000.0, is_buy=True)

        assert r.closed_lots == []


class TestBreakeven:
    def test_the_stop_moves_to_entry_and_latches(self):
        r = sim.simulate_ticks(
            _tpl(tp1_pips=20.0, tp1_pct=50.0, tp2_pips=40.0, tp2_pct=50.0,
                 be_trigger=1),
            _ticks(4002.5, 4000.2, 3998.0),
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4000.0)


class TestTrailing:
    def test_step_trails_with_the_step_gate(self):
        r = sim.simulate_ticks(
            _tpl(trail_mode="step", trail_distance=20.0, trail_activation=10.0,
                 trail_step=10.0),
            _ticks(4005.0, 4006.0, 3994.0),
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4004.0)

    def test_tp_mode_trails_to_the_highest_cleared_tp(self):
        r = sim.simulate_ticks(
            _tpl(trail_mode="tp", trail_activation=10.0,
                 tp1_pips=20.0, tp1_pct=0.0, tp2_pips=40.0, tp2_pct=0.0),
            _ticks(4002.1, 3999.0),
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4002.0)


class TestItRefusesWhatItCannotModel:
    def test_a_grid_template_is_not_simulated(self):
        with pytest.raises(sim.UnsupportedTemplate):
            sim.simulate_ticks(_tpl(mode="grid"), _ticks(4001.0),
                               entry=4000.0, is_buy=True)

    @pytest.mark.parametrize("mode", ["staged", "fractal"])
    def test_the_bar_walks_unmodelled_trail_modes_stay_refused(self, mode):
        with pytest.raises(sim.UnsupportedTemplate):
            sim.simulate_ticks(_tpl(trail_mode=mode), _ticks(4001.0),
                               entry=4000.0, is_buy=True)

    def test_candle_trail_is_ALSO_refused_on_ticks(self):
        """Unlike the bar walk (which approximates it from the previous
        bar), ticks have no candle series at all -- CandleTrailLevel() needs
        the last 3 closed M15 candles, mql5:2558-2576."""
        with pytest.raises(sim.UnsupportedTemplate) as e:
            sim.simulate_ticks(_tpl(trail_mode="candle"), _ticks(4001.0),
                               entry=4000.0, is_buy=True)

        assert "candle" in str(e.value)

    def test_candle_trail_still_works_on_the_bar_walk(self):
        """Negative control: this refusal is tick-specific, not a global
        regression of the bar walk's own candle-trail support."""
        r = sim.simulate(
            _tpl(trail_mode="candle", trail_activation=10.0),
            [{"high": 4005.0, "low": 4001.0, "open": 4001.0, "close": 4005.0, "time": 0},
             {"high": 4006.0, "low": 4002.0, "open": 4002.0, "close": 4006.0, "time": 1},
             {"high": 4004.0, "low": 4000.5, "open": 4000.5, "close": 4004.0, "time": 2}],
            entry=4000.0, is_buy=True)

        assert r.close_price == pytest.approx(4001.0)


# ---------------------------------------------------------------------------
# Everything below pins simulate_ticks to ManageTemplate() and MoveSl() in
# mql5/ForexTraderBridge.mq5, read line by line. Replaying real trades on
# 2026-11-06 showed the walk understating `Asian - Single` because it moved
# the stop straight onto a TP the EA would have clamped away from price.
# ---------------------------------------------------------------------------

_LADDER = dict(tp1_pips=20.0, tp1_pct=50.0, tp2_pips=100.0, tp2_pct=50.0)
_BE1 = dict(_LADDER, be_trigger=1)


def _walk(ticks, is_buy=True, stops_level_points=0.0, **over):
    return sim.simulate_ticks(_tpl(**over), _ticks(*ticks), entry=4000.0,
                              is_buy=is_buy,
                              stops_level_points=stops_level_points)


class TestMoveSlGuardClamp:
    """MoveSl: minDist = max(stops level, guard_pips, safety_cap_pips), and a
    stop closer to price than that is pulled back to exactly that distance."""

    @pytest.mark.parametrize("guard,cap", [(30.0, 0.0), (0.0, 30.0), (30.0, 10.0), (10.0, 30.0)])
    def test_the_larger_of_guard_and_cap_holds_the_breakeven_off_price(self, guard, cap):
        """TP1 clears at 4002.1, 3.0 of guard: 3999.1, not entry."""
        r = _walk([4002.1, 3999.0], guard_pips=guard, safety_cap_pips=cap, **_BE1)

        assert r.close_price == pytest.approx(3999.1)

    def test_the_brokers_stops_level_is_a_floor_of_its_own(self):
        """It is in points: 300 x 0.01 = 3.0, with no guard configured."""
        r = _walk([4002.1, 3999.0], stops_level_points=300.0, **_BE1)

        assert r.close_price == pytest.approx(3999.1)

    def test_a_sell_is_clamped_above_the_ask(self):
        r = _walk([(3997.8, 3997.9), (4000.8, 4001.0)], is_buy=False,
                  guard_pips=30.0, **_BE1)

        assert r.close_price == pytest.approx(4000.9)

    def test_a_clamped_breakeven_counts_as_done_and_is_never_retried(self):
        """MoveSl returns true for the clamped stop, so tplBeDone latches: the
        stop stays below entry even after price makes room for entry itself."""
        r = _walk([4002.1, 4009.0, 3999.0], guard_pips=30.0, **_BE1)

        assert r.close_price == pytest.approx(3999.1)

    def test_a_clamp_that_is_not_better_leaves_breakeven_armed(self):
        """8.0 of guard at 4002.1 is 3994.1, behind the 3995 stop: MoveSl
        returns false, tplBeDone stays false, and the latched level retries."""
        r = _walk([4002.1, 4009.0, 3999.0], guard_pips=80.0, **_BE1)

        assert r.close_price == pytest.approx(4000.0)

    def test_the_clamp_leaves_a_stop_already_far_enough_from_price(self):
        """Negative control: with room for it, the stop goes exactly to entry
        and the clamp changes nothing."""
        r = _walk([4009.0, 3999.0], guard_pips=30.0, **_BE1)

        assert r.close_price == pytest.approx(4000.0)

    def test_with_no_floor_a_stop_beyond_price_is_refused_not_clamped(self):
        """minDist 0 skips the clamp, and the broker rejects a stop on the far
        side of price. A buffer of 3.0 at 4002.1 would otherwise close the
        trade at 4003 on the next tick, a price the market never offered."""
        r = _walk([4002.1, 4001.0, 3994.0], be_mode="entry_buffer",
                  be_buffer_pts=3.0, **_BE1)

        assert r.close_price == pytest.approx(3995.0)

    def test_a_refused_buffer_retries_once_price_makes_room(self):
        r = _walk([4002.1, 4004.0, 4002.5], be_mode="entry_buffer",
                  be_buffer_pts=3.0, **_BE1)

        assert r.close_price == pytest.approx(4003.0)


class TestTpTrailReadsTheLivePrice:
    def test_a_just_cleared_tp_is_clamped_instead_of_closing_the_trade_early(self):
        """The defect found on `Asian - Single`. 1.0 of guard: 4002.1 puts the
        stop at 4001.1, so 4001.5 is not a stop-out; 4003.0 makes room for TP1
        itself, and the next retrace closes there."""
        r = _walk([4002.1, 4001.5, 4003.0, 4001.0], trail_mode="tp",
                  trail_activation=10.0, guard_pips=10.0, **_LADDER)

        assert (r.close_bar, r.close_price) == (3, pytest.approx(4002.0))

    def test_the_best_level_is_the_last_one_price_is_beyond(self):
        """Negative control on the walk: TP2 at 4010 has not cleared."""
        r = _walk([4005.0, 4001.0], trail_mode="tp", trail_activation=10.0,
                  **_LADDER)

        assert r.close_price == pytest.approx(4002.0)

    @pytest.mark.parametrize("over", [{"partials": 0}, {"tpsl_mode": "off"}])
    def test_it_trails_a_level_that_never_traded(self, over):
        """The EA's walk is TpCleared, not triggered[]: partials off and
        tpsl_mode off never latch a level, and the trail moves regardless."""
        r = _walk([4002.1, 3999.0], trail_mode="tp", trail_activation=10.0,
                  **_LADDER, **over)

        assert r.close_price == pytest.approx(4002.0)


class TestTrailArming:
    def test_no_activation_means_armed_from_the_first_tick(self):
        r = _walk([4001.0, 3998.9], trail_mode="step", trail_distance=20.0)

        assert r.close_price == pytest.approx(3999.0)

    def test_no_activation_ignores_the_tp_level_trigger(self):
        """The level trigger is only consulted when an activation distance
        exists and has not been reached."""
        r = _walk([4001.0, 3998.9], trail_mode="step", trail_distance=20.0,
                  tp1_trigger_level=1, **_LADDER)

        assert r.close_price == pytest.approx(3999.0)

    def test_an_unreached_activation_holds_the_trail_back(self):
        """Negative control for the two above."""
        r = _walk([4001.0, 3998.9, 3994.0], trail_mode="step",
                  trail_distance=20.0, trail_activation=100.0)

        assert r.close_price == pytest.approx(3995.0)

    def test_the_level_trigger_arms_off_a_level_that_traded(self):
        r = _walk([4002.1, 3999.0], trail_mode="step", trail_distance=20.0,
                  trail_activation=1000.0, tp1_trigger_level=1, **_LADDER)

        assert r.close_price == pytest.approx(4000.1)

    def test_a_zero_lot_level_does_not_arm_the_level_trigger(self):
        """DoPartialClose returns before setting triggered[] when it has no
        lots to close, so tp1_pct=0 leaves the trigger unarmed for good."""
        r = _walk([4002.1, 3999.0, 3994.0], trail_mode="step",
                  trail_distance=20.0, trail_activation=1000.0,
                  tp1_trigger_level=1, tp1_pips=20.0, tp1_pct=0.0,
                  tp2_pips=100.0, tp2_pct=50.0)

        assert r.close_price == pytest.approx(3995.0)


class TestStepTrail:
    def test_a_missing_distance_falls_back_to_the_eas_default(self):
        """InpDefaultTrailStopPts is 5.0 of raw price, not pips."""
        r = _walk([4006.0, 4000.9], trail_mode="step", trail_distance=0.0)

        assert r.close_price == pytest.approx(4001.0)

    def test_padding_widens_the_distance(self):
        r = _walk([4006.0, 4003.0], trail_mode="step", trail_distance=20.0,
                  trail_padding=10.0)

        assert r.close_price == pytest.approx(4003.0)

    def test_the_step_gate_measures_before_the_clamp(self):
        """dist 0.5, guard 1.0, step 1.0. 4003.0 stops at 4002.0 (clamped from
        4002.5). 4003.3 wants 4002.8, only 0.8 better: no move. 4003.6 wants
        4003.1, 1.1 better: moves, and lands clamped at 4002.6, only 0.6 above
        the old stop. A gate read after the clamp would have refused it."""
        r = _walk([4003.0, 4003.3, 4003.6, 4002.5], trail_mode="step",
                  trail_distance=5.0, trail_step=10.0, guard_pips=10.0)

        assert r.close_price == pytest.approx(4002.6)

    def test_the_step_gate_holds_a_small_improvement(self):
        """Negative control: the same walk without the third tick closes at
        the stop the gate left in place."""
        r = _walk([4003.0, 4003.3, 4001.9], trail_mode="step",
                  trail_distance=5.0, trail_step=10.0, guard_pips=10.0)

        assert r.close_price == pytest.approx(4002.0)

    def test_a_buy_with_no_stop_can_be_given_one(self):
        r = _walk([4003.0, 4000.9], trail_mode="step", trail_distance=20.0,
                  sl_pips=0.0)

        assert (r.outcome, r.close_price) == ("sl", pytest.approx(4001.0))

    def test_a_sell_with_no_stop_is_never_given_one(self):
        """MoveSl compares against POSITION_SL, 0.0 when there is none, and
        `newSl < 0` is never true. An EA quirk, reproduced because the walk
        must measure what the EA does: the trade runs to the end of the data."""
        r = _walk([(3996.9, 3997.0), (4050.0, 4050.1)], is_buy=False,
                  trail_mode="step", trail_distance=20.0, sl_pips=0.0)

        assert r.outcome == "timeout"


class TestFillPrices:
    """DoPartialClose and DoCloseAll trade at market. Only the resting broker
    TP, sent for tpsl_mode=on with close_full_on_last, fills at its level."""

    def test_a_partial_fills_at_the_tick_not_the_level(self):
        """Half at 4003.0 (+3.0), half stopped at 3995 (-5.0). The level alone
        would have banked +2.0 on the first half."""
        r = _walk([4003.0, 3994.0], **_LADDER)

        assert r.pnl_price == pytest.approx(0.05 * 3.0 - 0.05 * 5.0)

    def test_a_sell_partial_fills_at_the_ask(self):
        r = _walk([(3996.9, 3997.0), (4005.0, 4005.1)], is_buy=False, **_LADDER)

        assert r.pnl_price == pytest.approx(0.05 * 3.0 - 0.05 * 5.0)

    def test_a_ladder_closed_out_by_partials_reports_the_tick(self):
        r = _walk([4003.0], tp1_pips=20.0, tp1_pct=100.0)

        assert (r.outcome, r.close_price) == ("tp", pytest.approx(4003.0))

    @pytest.mark.parametrize("over", [
        {"tpsl_mode": "stealth", "close_full_on_last": 1},
        {"tpsl_mode": "on", "close_full_on_last": 0, "partials": 0},
    ])
    def test_a_last_level_with_no_broker_tp_closes_at_market(self, over):
        r = _walk([4003.0], tp1_pips=20.0, tp1_pct=50.0, **over)

        assert r.close_price == pytest.approx(4003.0)

    @pytest.mark.parametrize("over", [{}, {"partials": 0}])
    def test_a_resting_broker_tp_fills_at_its_level(self, over):
        """Negative control: HandleOpenTrade sets the native TP for exactly
        this combination, and it fills server-side at the level."""
        r = _walk([4003.0], tp1_pips=20.0, tp1_pct=50.0, tpsl_mode="on",
                  close_full_on_last=1, **over)

        assert r.close_price == pytest.approx(4002.0)


class TestBreakevenArming:
    @pytest.mark.parametrize("over", [{"partials": 0}, {"tpsl_mode": "off"}])
    def test_a_level_that_never_trades_arms_it_off_the_live_price(self, over):
        """`triggered[beIdx] || TpCleared(...)`: the fallback exists for these
        two modes, where nothing ever sets triggered[]."""
        r = _walk([4002.1, 3999.0], **_BE1, **over)

        assert r.close_price == pytest.approx(4000.0)

    def test_it_stays_unarmed_below_the_level(self):
        """Negative control on the fallback."""
        r = _walk([4001.9, 3994.0], partials=0, **_BE1)

        assert r.close_price == pytest.approx(3995.0)

    def test_the_trigger_names_a_tp_number_not_a_ladder_position(self):
        """tp[] keeps its gaps on the EA side. With TP1 and TP3 defined,
        be_trigger=2 points at a level that does not exist and never arms."""
        r = _walk([4006.5, 3994.0], be_trigger=2, tp1_pips=20.0, tp1_pct=25.0,
                  tp3_pips=60.0, tp3_pct=25.0)

        assert r.close_price == pytest.approx(3995.0)

    def test_the_trigger_finds_a_level_after_a_gap(self):
        r = _walk([4006.5, 3999.0], be_trigger=3, tp1_pips=20.0, tp1_pct=25.0,
                  tp3_pips=60.0, tp3_pct=25.0)

        assert r.close_price == pytest.approx(4000.0)
