"""Order flow, scoped to what this feed can actually answer.

Section 4.3 of docs/todo/reversal-engine/200. Professional gold desks read
cumulative delta, footprint imbalance and absorption at the level. Two things
stood between this app and any of that:

  1. the bridge requested COPY_TICKS_ALL and then threw `flags`, `last` and
     `volume` away in a dict comprehension. Fixed 2026-09-11.
  2. a retail spot-CFD feed often publishes bid/ask quotes only, with no
     trade side at all, in which case true delta does not exist and any CVD
     is a tick-rule proxy.

So the first function here is a PROBE, not a calculation. Building a delta
feature on an assumption about a broker's tick stream is how a model ends up
learning from a constant.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import order_flow as of


def quote(ts, bid, ask, flags=of.TICK_FLAG_BID | of.TICK_FLAG_ASK, **kw):
    t = {"time": ts, "bid": bid, "ask": ask, "flags": flags}
    t.update(kw)
    return t


def trade(ts, bid, ask, side, volume=1.0):
    flag = of.TICK_FLAG_BUY if side == "BUY" else of.TICK_FLAG_SELL
    return {"time": ts, "bid": bid, "ask": ask, "last": (ask if side == "BUY" else bid),
            "volume": volume, "flags": of.TICK_FLAG_LAST | of.TICK_FLAG_VOLUME | flag}


class TestTheProbe:
    def test_a_quote_only_feed_reports_no_trade_side(self):
        ticks = [quote(1, 3300.0, 3300.4), quote(2, 3300.1, 3300.5)]
        cap = of.probe_feed(ticks)
        assert cap.has_trade_side is False
        assert cap.has_volume is False
        assert cap.n_ticks == 2

    def test_a_feed_carrying_buy_and_sell_flags_reports_trade_side(self):
        cap = of.probe_feed([trade(1, 3300.0, 3300.4, "BUY"),
                             trade(2, 3300.0, 3300.4, "SELL")])
        assert cap.has_trade_side is True
        assert cap.has_volume is True

    def test_an_empty_probe_claims_nothing(self):
        cap = of.probe_feed([])
        assert cap.has_trade_side is False
        assert cap.n_ticks == 0
        assert "no ticks" in cap.note.lower()


class TestCumulativeDelta:
    def test_with_real_trade_sides_it_sums_signed_volume(self):
        ticks = [trade(1, 3300.0, 3300.4, "BUY", volume=3.0),
                 trade(2, 3300.0, 3300.4, "SELL", volume=1.0)]
        r = of.cumulative_delta(ticks)
        assert r.method == "trade_flags"
        assert r.delta == pytest.approx(2.0)

    def test_without_trade_sides_it_falls_back_to_the_tick_rule_and_says_so(self):
        """An uptick is classified as buyer-initiated. It is a PROXY, and
        the method field is what stops it being quoted as measured delta."""
        ticks = [quote(1, 3300.0, 3300.4), quote(2, 3300.2, 3300.6),
                 quote(3, 3300.1, 3300.5)]
        r = of.cumulative_delta(ticks)
        assert r.method == "tick_rule"
        assert r.delta == pytest.approx(0.0)

    def test_the_tick_rule_ignores_unchanged_mids(self):
        ticks = [quote(1, 3300.0, 3300.4), quote(2, 3300.0, 3300.4),
                 quote(3, 3300.2, 3300.6)]
        assert of.cumulative_delta(ticks).delta == pytest.approx(1.0)

    def test_one_tick_gives_no_delta_rather_than_zero(self):
        """Zero would mean balanced flow. One tick is no observation of flow
        at all, and the two must not read the same in a feature vector."""
        assert of.cumulative_delta([quote(1, 3300.0, 3300.4)]) is None

    def test_rolling_delta_buckets_by_time_so_divergence_can_be_measured(self):
        ticks = [quote(i, 3300.0 + i * 0.1, 3300.4 + i * 0.1) for i in range(1, 7)]
        buckets = of.rolling_delta(ticks, bucket_s=3.0)
        assert len(buckets) == 2
        assert all(b.delta > 0 for b in buckets)


class TestActivity:
    def test_arrival_rate_is_ticks_per_second_over_the_window(self):
        ticks = [quote(t, 3300.0, 3300.4) for t in (10, 11, 12, 13, 14)]
        assert of.tick_arrival_rate(ticks, window_s=4.0) == pytest.approx(1.25)

    def test_a_zero_window_cannot_produce_a_rate(self):
        assert of.tick_arrival_rate([quote(1, 3300.0, 3300.4)], window_s=0.0) is None


class TestSpread:
    def test_it_reports_the_mean_and_the_worst_spread(self):
        ticks = [quote(1, 3300.0, 3300.2), quote(2, 3300.0, 3300.6)]
        s = of.spread_stats(ticks)
        assert s.mean_pts == pytest.approx(0.4)
        assert s.max_pts == pytest.approx(0.6)

    def test_widening_into_the_level_is_flagged(self):
        """A spread widening as price approaches is liquidity being pulled,
        and it is also your cost. On a 5.75 point stop a 0.6 point spread is
        over 10% of R."""
        ticks = ([quote(i, 3300.0, 3300.2) for i in range(1, 11)]
                 + [quote(i, 3300.0, 3301.0) for i in range(11, 21)])
        assert of.spread_stats(ticks).widening_ratio > 2.0

    def test_a_crossed_or_zero_quote_is_excluded_rather_than_averaged(self):
        ticks = [quote(1, 3300.0, 3300.2), quote(2, 0.0, 3300.6),
                 quote(3, 3301.0, 3300.0)]
        assert of.spread_stats(ticks).n == 1
