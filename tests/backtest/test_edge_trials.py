"""The 2025 edge trials (docs/todo/reversal-engine/270).

Pure and offline. Pinned: the bar cannot pass what the spec fails, the
clustered t does not count one day's trades as independent evidence, and each
new trial reads only bars from before its decision and trades the direction
its rule names.
"""
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.src.services.backtest import edge_trials as et
from backend.src.services.reversal_engine.entry_study import Bar

NY = ZoneInfo("America/New_York")


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def _ny(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()


def _trades(n_days, per_day, mean, sd=1.0, start=(2025, 1, 2), seed=1):
    rng = random.Random(seed)
    t0 = _utc(*start, 12)
    out = []
    for d in range(n_days):
        for _ in range(per_day):
            out.append({"ts": t0 + d * 86400, "r": rng.gauss(mean, sd)})
    return out


class TestTheBar:
    def test_a_strong_steady_edge_is_a_candidate(self):
        j = et.judge(_trades(300, 1, 0.3))
        assert j["candidate"], j["why"]

    def test_it_reports_the_spread_of_results_for_the_deflated_sharpe(self):
        j = et.judge(_trades(400, 1, 0.0, sd=2.0, seed=9))
        assert j["sd"] == pytest.approx(2.0, rel=0.1)

    def test_no_edge_is_not(self):
        j = et.judge(_trades(300, 1, 0.0))
        assert not j["candidate"]

    def test_fewer_than_50_trades_is_not_even_with_a_large_mean(self):
        j = et.judge(_trades(40, 1, 2.0, sd=0.5))
        assert not j["candidate"] and any("n " in w for w in j["why"])

    def test_an_edge_in_one_half_only_is_not(self):
        tr = _trades(360, 1, 0.6)
        for x in tr:
            if x["ts"] >= et.HALF_SPLIT_2025:
                x["r"] = -0.05
        j = et.judge(tr)
        assert not j["candidate"] and any("half" in w for w in j["why"])

    def test_the_t_bar_is_bonferroni_for_nine_trials(self):
        assert et.T_BAR == pytest.approx(2.54, abs=0.01)


class TestClusteredT:
    def test_ten_copies_of_one_days_trade_are_not_ten_times_the_evidence(self):
        one = _trades(100, 1, 0.2, seed=4)
        ten = [dict(x) for x in one for _ in range(10)]
        assert et.clustered_t(ten) == pytest.approx(et.clustered_t(one), rel=0.05)

    def test_independent_days_do_add_evidence(self):
        assert et.clustered_t(_trades(400, 1, 0.2, seed=5)) > et.clustered_t(_trades(100, 1, 0.2, seed=5))


def _m1_day(y, mo, d, path):
    """M1 bars from `path`: {utc_ts: price}, flat between points."""
    return [Bar(ts, p, p + 0.1, p - 0.1, p, 1.0) for ts, p in sorted(path.items())]


def _flat_minutes(t_from, t_to, px):
    out, t = {}, t_from
    while t < t_to:
        out[t] = px
        t += 60
    return out


class TestIntradayMomentum:
    def _day(self, first_move, last_move):
        y, mo, d = 2025, 3, 12            # a Wednesday, US daylight time
        o = _ny(y, mo, d, 8, 20)
        path = _flat_minutes(o - 3600, _ny(y, mo, d, 14, 0), 3000.0)
        for t in path:
            if t >= _ny(y, mo, d, 8, 49):
                path[t] = 3000.0 + first_move
        for t in path:
            if t >= _ny(y, mo, d, 13, 15):
                path[t] = 3000.0 + first_move + last_move
        return _m1_day(y, mo, d, path)

    def test_an_up_first_half_hour_buys_into_settlement(self):
        tr = et.intraday_momentum(self._day(+5.0, +2.0), cost=0.5)
        assert len(tr) == 1
        assert tr[0]["side"] == 1 and tr[0]["r"] == pytest.approx(2.0 - 0.5)

    def test_a_down_first_half_hour_sells(self):
        tr = et.intraday_momentum(self._day(-5.0, +2.0), cost=0.5)
        assert tr[0]["side"] == -1 and tr[0]["r"] == pytest.approx(-2.0 - 0.5)

    def test_a_flat_first_half_hour_is_no_trade(self):
        assert et.intraday_momentum(self._day(0.0, 3.0), cost=0.5) == []


class TestAsianSweepFade:
    def _m5(self, bars):
        return [Bar(ts, o, h, l, c, 1.0) for ts, o, h, l, c in bars]

    def _asia(self, y=2025, mo=3, d=12):
        return [(_utc(y, mo, d, 0) + 300 * i, 3000, 3005, 2995, 3000) for i in range(84)]

    def test_a_poke_above_asia_that_closes_back_inside_is_sold(self):
        t7 = _utc(2025, 3, 12, 7)
        bars = self._m5(self._asia() + [
            (t7, 3003, 3008, 3002, 3004),       # sweeps 3005, closes back inside
            (t7 + 300, 3004, 3004.5, 2999, 2999),  # 1R = 4.0 below 3004 -> 3000
        ])
        tr = et.asian_sweep_fade(bars, cost=0.0)
        assert len(tr) == 1
        assert tr[0]["side"] == -1
        assert tr[0]["r"] == pytest.approx(1.0)

    def test_a_close_beyond_asia_is_a_breakout_not_a_sweep(self):
        t7 = _utc(2025, 3, 12, 7)
        bars = self._m5(self._asia() + [(t7, 3003, 3008, 3002, 3007)])
        assert et.asian_sweep_fade(bars, cost=0.0) == []

    def test_a_bar_that_spans_stop_and_target_is_a_loss(self):
        t7 = _utc(2025, 3, 12, 7)
        bars = self._m5(self._asia() + [
            (t7, 3003, 3008, 3002, 3004),
            (t7 + 300, 3004, 3009, 2990, 3000),
        ])
        assert et.asian_sweep_fade(bars, cost=0.0)[0]["r"] == pytest.approx(-1.0)


class TestDailyMomentum:
    def test_it_trades_the_sign_of_the_last_20_days_and_never_reads_today(self):
        start = _utc(2025, 1, 1, 12)
        closes = [3000 + i for i in range(25)] + [3024, 3000]
        bars = [Bar(start + 86400 * i, c, c, c, c, 1.0) for i, c in enumerate(closes)]
        tr = et.daily_momentum(bars, lookback=20, cost=0.0)
        # The day that fell 24 points was traded long: the past 20 days were up.
        last = tr[-1]
        assert last["side"] == 1 and last["r"] == pytest.approx(-24.0)
        assert len(tr) == len(closes) - 21


class TestOrbNyDriver:
    def test_production_rules_are_used_and_the_trend_filter_can_be_switched_off(self):
        # A day whose breakout is UP while the H4 trend is DOWN: the production
        # rules skip it, the no-filter variant takes it.
        y, mo, d = 2025, 3, 12
        o = _ny(y, mo, d, 9, 30)
        m5 = []
        for i in range(6):  # the opening range, 3000-3004
            m5.append(Bar(o + 300 * i, 3002, 3004, 3000, 3002, 1.0))
        m5.append(Bar(o + 1800, 3002, 3004.6, 3002, 3004.5))     # closes 0.5 above
        for i in range(1, 40):
            m5.append(Bar(o + 1800 + 300 * i, 3004.5, 3014, 3004.2, 3013, 1.0))
        h4_down = [Bar(_utc(y, mo, 1) + 14400 * i, 0, 0, 0, 3100 - i, 1.0) for i in range(60)]
        with_filter = et.orb_ny_trades(m5, h4_down, trend_filter=True, cost=0.0)
        without = et.orb_ny_trades(m5, h4_down, trend_filter=False, cost=0.0)
        assert with_filter == []
        assert len(without) == 1 and without[0]["side"] == 1
        assert without[0]["r"] == pytest.approx(2.0)


class TestTheHoldoutBar:
    """Amendment A (270): pooled t >= 2.13, positive in more than two thirds of
    the years with >= 30 trades."""

    def _years(self, means, per_year=60, sd=1.0, seed=2):
        rng = random.Random(seed)
        out = []
        for y, m in means.items():
            t0 = _utc(y, 1, 3, 15)
            for i in range(per_year):
                out.append({"ts": t0 + i * 5 * 86400 / per_year * 60, "r": rng.gauss(m, sd)})
        return out

    def test_a_steady_edge_across_years_passes(self):
        tr = self._years({y: 0.35 for y in range(2019, 2025)})
        j = et.holdout_judge(tr)
        assert j["passes"], j["why"]
        assert set(j["years"]) == set(range(2019, 2025))

    def test_an_edge_in_two_years_of_six_fails_on_years(self):
        means = {2019: 1.2, 2020: 1.2, 2021: -0.05, 2022: -0.05, 2023: -0.05, 2024: -0.05}
        j = et.holdout_judge(self._years(means))
        assert not j["passes"] and any("year" in w for w in j["why"])

    def test_the_holdout_t_bar_is_bonferroni_for_three(self):
        assert et.HOLDOUT_T_BAR == pytest.approx(2.13, abs=0.01)

    def test_a_year_with_under_30_trades_does_not_count(self):
        tr = self._years({y: 0.35 for y in range(2019, 2025)})
        tr += [{"ts": _utc(2018, 6, 1, 15) + i * 86400, "r": -3.0} for i in range(10)]
        j = et.holdout_judge(tr)
        assert 2018 not in j["counted_years"]


class TestReversedPoints:
    def test_reversing_a_points_trade_swaps_the_move_and_still_pays_the_cost(self):
        # Long, the market fell 2.0, cost 0.5: original r = -2.5. Reversed is
        # short the same move: +2.0 - 0.5 = +1.5.
        tr = et.reverse_points([{"ts": 1.0, "side": 1, "r": -2.5}], cost=0.5)
        assert tr[0]["side"] == -1 and tr[0]["r"] == pytest.approx(1.5)
