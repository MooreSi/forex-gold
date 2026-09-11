"""Transaction cost analysis: what each fill actually cost.

Section 5.1 of docs/todo/reversal-engine/200. The app currently has no
measured execution cost at all. `services/trading/fees_sizing.py` charges
`estimated_slippage_points` -- a CONSTANT from fee settings, default 5.0 --
to every trade regardless of what happened, and the spread it charges is
whatever was passed in at open. Nothing anywhere compares the price asked for
with the price received.

That matters because of the size of it. On the reversal engine's mean 5.75
point stop, a 0.6 point round trip is over 10% of R, and item 020 is trying
to explain 0.53 points of leakage per loss without measuring this at all.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker import tca


class _Bridge:
    def __init__(self, ticks_at=None):
        self.ticks_at = ticks_at or {}
        self.asked: list[float] = []

    async def get_tick_at(self, ts):
        self.asked.append(ts)
        return self.ticks_at.get(round(ts, 3))


def _trade(**kw):
    base = {"trade_id": "T1", "direction": "BUY", "entry_price": 3300.5,
            "open_time": 1000.0, "close_time": 1600.0, "close_price": 3310.0,
            "lot_size": 0.10, "strategy": "reversal_engine"}
    base.update(kw)
    return base


class TestSlippage:
    def test_a_buy_filled_above_the_asked_price_slipped_against_us(self):
        cost = tca.slippage_pts("BUY", requested=3300.0, filled=3300.5)
        assert cost == pytest.approx(0.5)

    def test_a_buy_filled_below_the_asked_price_slipped_in_our_favour(self):
        """Signed, not absolute. Positive slippage is real and a system that
        only ever reports the adverse half overstates its own costs, which is
        just as misleading as understating them."""
        assert tca.slippage_pts("BUY", requested=3300.0, filled=3299.8) == pytest.approx(-0.2)

    def test_a_sell_is_the_mirror_image(self):
        assert tca.slippage_pts("SELL", requested=3300.0, filled=3299.5) == pytest.approx(0.5)


class TestSpreadIsMeasuredNotAssumed:
    def test_the_spread_at_the_fill_comes_from_the_tick_at_that_moment(self):
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.6}})
        c = asyncio.run(tca.measure(bridge, _trade(), requested_price=3300.4,
                                    sl_dist=5.0))
        assert c.spread_open_pts == pytest.approx(0.4)
        assert c.spread_close_pts == pytest.approx(0.6)

    def test_the_round_trip_cost_is_half_the_spread_at_each_end(self):
        """Buy at the ask and sell at the bid. Measured against mid to mid,
        that is half a spread going in and half coming out -- not a full
        spread twice, which would double-count and make every strategy look
        worse than it is."""
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.6}})
        c = asyncio.run(tca.measure(bridge, _trade(), requested_price=3300.4,
                                    sl_dist=5.0))
        assert c.spread_cost_pts == pytest.approx(0.5)

    def test_a_missing_tick_leaves_the_spread_unknown_rather_than_zero(self):
        """Zero would say this fill was free. It was not; it was unmeasured,
        and an average that silently includes free fills is the wrong number
        to take into a decision about stop width."""
        c = asyncio.run(tca.measure(_Bridge({}), _trade(),
                                    requested_price=3300.4, sl_dist=5.0))
        assert c.spread_open_pts is None
        assert c.cost_pts is None
        assert c.measured is False


class TestCostInR:
    def test_cost_is_expressed_against_the_stop_that_defined_r(self):
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.4}})
        c = asyncio.run(tca.measure(bridge, _trade(), requested_price=3300.0,
                                    sl_dist=5.0))
        # 0.5 slippage + 0.4 half-spreads = 0.9 points against a 5.0 stop.
        assert c.cost_pts == pytest.approx(0.9)
        assert c.cost_r == pytest.approx(0.18)

    def test_a_zero_stop_leaves_cost_in_r_unknown_rather_than_infinite(self):
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.4}})
        c = asyncio.run(tca.measure(bridge, _trade(), requested_price=3300.0,
                                    sl_dist=0.0))
        assert c.cost_r is None


class TestFillDelay:
    def test_the_delay_between_decision_and_fill_is_recorded(self):
        """Item 040 found sub-five-minute fills lose $2,142. Delay is the
        axis that finding lives on, so every cost row carries it."""
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.4}})
        c = asyncio.run(tca.measure(bridge, _trade(), requested_price=3300.0,
                                    sl_dist=5.0, decision_ts=940.0))
        assert c.fill_delay_s == pytest.approx(60.0)


class TestAggregation:
    def _c(self, cost_r, key, measured=True):
        return tca.FillCost(trade_id="x", direction="BUY", open_time=0.0,
                            requested_price=0.0, fill_price=0.0,
                            slippage_pts=0.0, broker_slippage_pts=0.0,
                            entry_drift_pts=0.0, spread_open_pts=0.0,
                            spread_close_pts=0.0, spread_cost_pts=0.0,
                            cost_pts=0.0, cost_r=cost_r, fill_delay_s=0.0,
                            measured=measured, bucket=key)

    def test_it_groups_and_reports_the_mean_cost_in_r(self):
        rows = [self._c(0.10, "london"), self._c(0.30, "london"),
                self._c(0.20, "asian")]
        out = tca.summarise(rows)
        assert out["london"]["mean_cost_r"] == pytest.approx(0.20)
        assert out["london"]["n"] == 2
        assert out["asian"]["mean_cost_r"] == pytest.approx(0.20)

    def test_unmeasured_fills_are_counted_separately_not_averaged_in(self):
        rows = [self._c(0.10, "london"), self._c(None, "london", measured=False)]
        out = tca.summarise(rows)
        assert out["london"]["n"] == 1
        assert out["london"]["unmeasured"] == 1


class TestTheCostSplitsIntoTwoDifferentProblems:
    """The 0.372R measured on 2026-09-11 was one number covering two causes
    with different fixes, and the study's own write-up had to caveat it.

      * **broker slippage** -- the fill against the price the broker was
        actually QUOTING at that instant. A execution-quality problem.
      * **entry drift** -- that quote against the price the decision was
        made at. Not the broker's doing at all: it is the signal chasing,
        or arriving late, or firing at market outside its own zone.

    They add up to the figure already reported, so nothing published
    changes meaning; it just becomes actionable.
    """

    def test_a_fill_at_the_quoted_ask_has_no_broker_slippage(self):
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.4}})
        c = asyncio.run(tca.measure(bridge, _trade(entry_price=3300.4),
                                    requested_price=3300.0, sl_dist=5.0))
        assert c.broker_slippage_pts == pytest.approx(0.0)
        assert c.entry_drift_pts == pytest.approx(0.4)

    def test_a_fill_worse_than_the_quote_is_broker_slippage(self):
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.4}})
        c = asyncio.run(tca.measure(bridge, _trade(entry_price=3300.9),
                                    requested_price=3300.0, sl_dist=5.0))
        assert c.broker_slippage_pts == pytest.approx(0.5)
        assert c.entry_drift_pts == pytest.approx(0.4)

    def test_a_sell_is_measured_against_the_bid(self):
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3290.0, "ask": 3290.4}})
        c = asyncio.run(tca.measure(bridge, _trade(direction="SELL",
                                                   entry_price=3299.8),
                                    requested_price=3300.0, sl_dist=5.0))
        # Sold at 3299.8 when the bid was 3300.0: 0.2 worse than quoted.
        assert c.broker_slippage_pts == pytest.approx(0.2)
        assert c.entry_drift_pts == pytest.approx(0.0)

    def test_the_two_parts_still_add_up_to_the_published_slippage(self):
        bridge = _Bridge({1000.0: {"bid": 3300.0, "ask": 3300.4},
                          1600.0: {"bid": 3310.0, "ask": 3310.4}})
        c = asyncio.run(tca.measure(bridge, _trade(entry_price=3301.1),
                                    requested_price=3300.0, sl_dist=5.0))
        assert (c.broker_slippage_pts + c.entry_drift_pts
                == pytest.approx(c.slippage_pts))

    def test_without_a_tick_at_the_fill_neither_part_is_guessed(self):
        """The total is still unknown too, so this is not a regression --
        it is the same refusal, now in three places instead of one."""
        c = asyncio.run(tca.measure(_Bridge({}), _trade(),
                                    requested_price=3300.0, sl_dist=5.0))
        assert c.broker_slippage_pts is None
        assert c.entry_drift_pts is None
