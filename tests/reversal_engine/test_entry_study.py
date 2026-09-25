"""Entry study: replaying signals on M1 bars against placebo entries.

docs/todo/reversal-engine/240, phase 1. Pure functions over synthetic bars.
Nothing here reaches a broker, a database or the network.

The properties pinned here are the ones that would make the study lie:
filling at a price the bar never offered, crediting a favourable move that
happened before the fill, reading a feature from bars after the entry, and a
placebo that does not come out at zero on a market with no edge in it.
"""
import random

import pytest

from backend.src.services.reversal_engine import entry_study as es

T0 = 1_790_000_000.0 - (1_790_000_000.0 % 60)


def _bar(i, o, h, l, c, v=100.0):
    return es.Bar(T0 + 60 * i, o, h, l, c, v)


def _flat(n, px=2000.0, start=0):
    return [_bar(start + i, px, px + 0.5, px - 0.5, px) for i in range(n)]


def _buy(created_i=0, lo=1999.0, hi=2001.0, level=2000.0):
    return es.Sig(1, T0 + 60 * created_i, "BUY", lo, hi, level)


def _sell(created_i=0, lo=1999.0, hi=2001.0, level=2000.0):
    return es.Sig(2, T0 + 60 * created_i, "SELL", lo, hi, level)


class TestTheTouch:
    def test_a_buy_approached_from_above_fills_at_the_top_of_the_zone(self):
        bars = [_bar(0, 2005, 2005.5, 2004, 2004.5),
                _bar(1, 2004.5, 2004.6, 2000.2, 2000.5)]
        e = es.find_touch(bars, _buy())
        assert e.idx == 1
        assert e.price == 2001.0

    def test_a_sell_approached_from_below_fills_at_the_bottom_of_the_zone(self):
        bars = [_bar(0, 1995, 1996, 1994.5, 1995.5),
                _bar(1, 1995.5, 1999.8, 1995.4, 1999.5)]
        e = es.find_touch(bars, _sell())
        assert e.price == 1999.0

    def test_a_bar_that_opens_inside_the_zone_fills_at_its_open(self):
        bars = [_bar(0, 2000.4, 2000.9, 1999.9, 2000.1)]
        assert es.find_touch(bars, _buy()).price == 2000.4

    def test_a_touch_before_the_signal_existed_does_not_count(self):
        # Bar 0 spans the zone, the signal is created at bar 5, and price
        # never comes back: no fill.
        bars = [_bar(0, 2000, 2000.5, 1999.5, 2000)] + \
               [_bar(i, 2010, 2010.5, 2009.5, 2010) for i in range(1, 20)]
        assert es.find_touch(bars, _buy(created_i=5)) is None

    def test_the_bar_the_signal_was_created_in_is_not_a_fill(self):
        # Created 30s into bar 0, which spans the zone. Its open and its
        # first half are from before the signal existed. Price then leaves.
        bars = [_bar(0, 2000.5, 2000.8, 1999.5, 2005)] + \
               [_bar(i, 2010, 2010.5, 2009.5, 2010) for i in range(1, 20)]
        sig = es.Sig(1, T0 + 30, "BUY", 1999.0, 2001.0, 2000.0)
        assert es.find_touch(bars, sig) is None

    def test_a_zone_not_reached_within_two_hours_is_an_expired_signal(self):
        bars = [_bar(i, 2010, 2010.5, 2009.5, 2010) for i in range(125)] + \
               [_bar(125, 2001, 2001, 2000, 2000)]
        assert es.find_touch(bars, _buy()) is None


class TestThePathAfterTheFill:
    def test_the_entry_bars_favourable_extreme_is_not_credited(self):
        # The bar printed 2008 BEFORE falling into the zone. A long filled at
        # 2001 must not be credited with a 7-point run it never had.
        bars = [_bar(0, 2008, 2008, 2000.5, 2000.6)]
        e = es.Entry(0, 2001.0, bars[0].ts)
        path = es.path_after(bars, e, "BUY")
        assert path[0][1] == 2001.0          # high capped at the fill
        assert path[0][2] == 2000.5          # adverse side kept

    def test_the_entry_bars_adverse_extreme_is_kept_for_a_sell(self):
        bars = [_bar(0, 1990, 1999.8, 1990, 1999.5)]
        e = es.Entry(0, 1999.0, bars[0].ts)
        path = es.path_after(bars, e, "SELL")
        assert path[0] == (bars[0].ts, 1999.8, 1999.0)

    def test_a_target_printed_before_the_fill_is_not_a_win(self):
        bars = [_bar(0, 2006, 2006, 2000.5, 2000.6)] + _flat(30, 2000.6, start=1)
        e = es.Entry(0, 2001.0, bars[0].ts)
        res = es.replay(bars, e, "BUY", es.barrier_policy(5.0, 4.0, 0.0))
        assert res.reason == "path_end"


class TestPlacebo:
    def _walk(self, n, seed):
        rng = random.Random(seed)
        px, bars = 2000.0, []
        for i in range(n):
            o = px
            steps = [rng.choice((-0.3, 0.3)) for _ in range(6)]
            path = [o]
            for s in steps:
                path.append(path[-1] + s)
            px = path[-1]
            bars.append(_bar(i, o, max(path), min(path), px))
        return bars

    def test_entries_chosen_at_random_on_a_driftless_walk_do_not_beat_it(self):
        # The negative control. If this drifts from zero the yardstick is
        # biased and every verdict the study prints is wrong.
        bars = self._walk(6000, seed=7)
        rng = random.Random(1)
        pol = es.barrier_policy(3.0, 3.0, 0.0)
        wins, placebo, blocks = [], [], []
        for i in rng.sample(range(800, 5000), 300):
            e = es.Entry(i, bars[i].open, bars[i].ts)
            w = es.won(es.replay(bars, e, "BUY", pol))
            p = es.placebo_rate(bars, e, "BUY", pol, rng, n=20, window_s=6 * 3600)
            if w is None or p is None:
                continue
            wins.append(w)
            placebo.append(p)
            blocks.append(bars[i].ts // 7200)
        z = es.beats_placebo(wins, placebo, blocks)["z"]
        assert abs(z) < 3.0

    def test_entries_placed_just_before_a_rise_do_beat_it(self):
        # The positive control: plant a 6-point rise after every chosen bar.
        bars = self._walk(6000, seed=11)
        chosen = list(range(900, 5000, 40))
        bars = list(bars)
        for i in chosen:
            base = bars[i].open
            for k in range(1, 4):
                px = base + 2.0 * k
                bars[i + k] = es.Bar(bars[i + k].ts, px - 2.0, px, px - 2.0, px)
        rng = random.Random(3)
        pol = es.barrier_policy(3.0, 3.0, 0.0)
        wins, placebo, blocks = [], [], []
        for i in chosen:
            e = es.Entry(i, bars[i].open, bars[i].ts)
            w = es.won(es.replay(bars, e, "BUY", pol))
            p = es.placebo_rate(bars, e, "BUY", pol, rng, n=20, window_s=6 * 3600)
            if w is None or p is None:
                continue
            wins.append(w)
            placebo.append(p)
            blocks.append(bars[i].ts // 7200)
        assert es.beats_placebo(wins, placebo, blocks)["z"] > 5.0


class TestFeatures:
    def _history(self, n=300, px=2010.0):
        return _flat(n, px)

    def test_price_falling_into_a_buy_reads_as_approaching(self):
        bars = self._history(290) + [
            _bar(290 + k, 2010 - k, 2010.2 - k, 2009.8 - k, 2010 - k - 1)
            for k in range(10)]
        e = es.Entry(len(bars) - 1, 2001.0, bars[-1].ts)
        f = es.features(bars, e, _buy())
        assert f["approach_5"] > 0

    def test_the_same_fall_into_a_sell_reads_as_moving_away(self):
        bars = self._history(290) + [
            _bar(290 + k, 2010 - k, 2010.2 - k, 2009.8 - k, 2010 - k - 1)
            for k in range(10)]
        e = es.Entry(len(bars) - 1, 1999.0, bars[-1].ts)
        f = es.features(bars, e, _sell())
        assert f["approach_5"] < 0

    def test_nothing_after_the_entry_changes_a_feature(self):
        bars = self._history(300)
        e = es.Entry(260, bars[260].open, bars[260].ts)
        before = es.features(bars, e, _buy())
        altered = bars[:261] + [es.Bar(b.ts, 3000, 3100, 2900, 3050, 99_999)
                                for b in bars[261:]]
        assert es.features(altered, e, _buy()) == before

    def test_the_entry_bar_itself_is_not_read(self):
        bars = self._history(300)
        e = es.Entry(260, bars[260].open, bars[260].ts)
        before = es.features(bars, e, _buy())
        bars[260] = es.Bar(bars[260].ts, 2010, 2500, 1500, 2010, 99_999)
        assert es.features(bars, e, _buy()) == before

    def test_less_than_four_hours_of_history_is_no_answer(self):
        bars = self._history(200)
        e = es.Entry(199, bars[199].open, bars[199].ts)
        assert es.features(bars, e, _buy()) is None

    def test_separate_visits_to_the_level_are_counted_once_each(self):
        bars = self._history(300, px=2010.0)
        for k in (10, 11, 12, 100, 200):      # 10-12 is one visit
            bars[k] = es.Bar(bars[k].ts, 2001, 2001, 1999.5, 2001)
        e = es.Entry(299, bars[299].open, bars[299].ts)
        assert es.features(bars, e, _buy())["touches_24h"] == 3.0


class TestConfirmation:
    def test_a_pierce_and_close_back_enters_at_the_next_open(self):
        bars = [_bar(0, 2001, 2001, 1999.2, 1999.4),     # touch, pierces 2000
                _bar(1, 1999.4, 2000.8, 1999.3, 2000.6),  # closes back above
                _bar(2, 2000.7, 2001, 2000.5, 2000.9)]
        touch = es.Entry(0, 2001.0, bars[0].ts)
        got = es.find_confirmation(bars, touch, _buy(), buffer_pts=0.5)
        assert got is not None
        entry, stop = got
        assert entry.idx == 2 and entry.price == 2000.7
        # extreme 1999.2, buffer 0.5 -> stop at 1998.7, 2.0 below 2000.7
        assert stop == pytest.approx(2.0)

    def test_closing_above_a_level_never_pierced_is_not_a_confirmation(self):
        bars = [_bar(0, 2001, 2001, 2000.2, 2000.8),
                _bar(1, 2000.8, 2001.5, 2000.3, 2001.2),
                _bar(2, 2001.2, 2001.5, 2001.0, 2001.3)]
        touch = es.Entry(0, 2001.0, bars[0].ts)
        assert es.find_confirmation(bars, touch, _buy()) is None

    def test_a_sell_mirrors_it(self):
        bars = [_bar(0, 1999, 2000.9, 1998.9, 2000.6),
                _bar(1, 2000.6, 2000.7, 1999.2, 1999.4),
                _bar(2, 1999.3, 1999.5, 1999.0, 1999.1)]
        touch = es.Entry(0, 1999.0, bars[0].ts)
        entry, stop = es.find_confirmation(bars, touch, _sell(), buffer_pts=0.5)
        assert entry.price == 1999.3
        assert stop == pytest.approx(2000.9 + 0.5 - 1999.3)

    def test_a_stop_wider_than_the_limit_is_no_trade(self):
        bars = [_bar(0, 2001, 2001, 1990.0, 1990.5),
                _bar(1, 1990.5, 2000.8, 1990.4, 2000.6),
                _bar(2, 2000.7, 2001, 2000.5, 2000.9)]
        touch = es.Entry(0, 2001.0, bars[0].ts)
        assert es.find_confirmation(bars, touch, _buy(), max_stop=8.0) is None


class TestTheStatistic:
    def test_winning_exactly_the_placebo_rate_is_z_zero(self):
        r = es.beats_placebo([True, False] * 50, [0.5] * 100)
        assert r["z"] == 0.0 and r["win_rate"] == 0.5

    def test_winning_every_trade_at_even_odds_is_a_large_z(self):
        r = es.beats_placebo([True] * 100, [0.5] * 100)
        assert r["z"] == pytest.approx(10.0)

    def test_trades_in_one_cluster_count_as_one_piece_of_evidence(self):
        # 100 wins that all rode the same move are one observation, not 100.
        alone = es.beats_placebo([True] * 100, [0.5] * 100)
        together = es.beats_placebo([True] * 100, [0.5] * 100, ["same"] * 100)
        assert together["z"] < alone["z"] / 5
        assert together["clusters"] == 1

    def test_on_independent_trades_it_agrees_with_the_textbook_variance(self):
        rng = random.Random(5)
        p = [0.3 + 0.4 * rng.random() for _ in range(4000)]
        w = [rng.random() < x for x in p]
        robust = sum(((1.0 if a else 0.0) - b) ** 2 for a, b in zip(w, p))
        textbook = sum(b * (1 - b) for b in p)
        assert robust == pytest.approx(textbook, rel=0.1)

    def test_mismatched_lengths_are_no_answer(self):
        assert es.beats_placebo([True], [0.5, 0.5])["z"] is None
