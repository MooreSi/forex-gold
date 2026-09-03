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
