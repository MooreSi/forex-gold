"""The levels an institutional desk actually references intraday.

Section 4.1 of docs/todo/reversal-engine/200. `level_detector` produces the
Asia range, H1 swings, round numbers, congestion and the ICT unicorn. It has
no previous-day high or low, no daily or weekly open, and no initial balance
-- the levels most reliably reacted to on gold, and all of them computable
from candles the bridge already serves.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import liquidity_map as lm

DAY = 86_400.0
# 2026-09-07 00:00:00 UTC. Verified a Monday, because the weekly-open tests
# are meaningless if it is not.
MON = 1_788_739_200.0


def d1(ts, o, h, l, cl):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": cl}


class TestPriorDay:
    def test_it_takes_the_most_recent_completed_day_not_todays_partial(self):
        candles = [d1(MON, 3290, 3310, 3280, 3300),
                   d1(MON + DAY, 3300, 3305, 3295, 3302)]
        levels = lm.prior_day_levels(candles, now=MON + DAY + 3600)
        assert levels["pdh"] == 3310
        assert levels["pdl"] == 3280
        assert levels["pdc"] == 3300

    def test_the_prior_day_midpoint_is_reported_too(self):
        candles = [d1(MON, 3290, 3310, 3280, 3300),
                   d1(MON + DAY, 3300, 3305, 3295, 3302)]
        assert lm.prior_day_levels(candles, now=MON + DAY + 3600)["pd_mid"] == 3295

    def test_todays_open_is_the_current_days_candle(self):
        candles = [d1(MON, 3290, 3310, 3280, 3300),
                   d1(MON + DAY, 3301, 3305, 3295, 3302)]
        assert lm.prior_day_levels(candles, now=MON + DAY + 3600)["daily_open"] == 3301

    def test_one_day_of_history_gives_no_prior_day(self):
        assert lm.prior_day_levels([d1(MON, 3290, 3310, 3280, 3300)],
                                   now=MON + 3600) is None


class TestPriorWeek:
    def test_it_spans_the_previous_calendar_week(self):
        last_week = [d1(MON - 7 * DAY + i * DAY, 3200, 3250 + i, 3150 - i, 3220)
                     for i in range(5)]
        this_week = [d1(MON + i * DAY, 3300, 3310, 3290, 3300) for i in range(2)]
        w = lm.prior_week_levels(last_week + this_week, now=MON + DAY)
        assert w["pwh"] == 3254
        assert w["pwl"] == 3146

    def test_the_weekly_open_is_this_weeks_first_candle(self):
        last_week = [d1(MON - 7 * DAY, 3200, 3250, 3150, 3220)]
        this_week = [d1(MON, 3301, 3310, 3290, 3300), d1(MON + DAY, 3300, 3311, 3288, 3305)]
        assert lm.prior_week_levels(last_week + this_week,
                                    now=MON + DAY)["weekly_open"] == 3301


class TestInitialBalance:
    def test_it_is_the_range_of_the_first_hour_of_the_session(self):
        m5 = [{"ts": MON + i * 300, "high": 3300 + i, "low": 3290 - i}
              for i in range(24)]
        ib = lm.initial_balance(m5, session_open_ts=MON, minutes=60)
        assert ib["ib_high"] == 3311
        assert ib["ib_low"] == 3279

    def test_candles_after_the_window_do_not_widen_it(self):
        m5 = [{"ts": MON + i * 300, "high": 3300 + i, "low": 3290 - i}
              for i in range(24)]
        m5.append({"ts": MON + 7200, "high": 3400, "low": 3200})
        assert lm.initial_balance(m5, session_open_ts=MON, minutes=60)["ib_high"] == 3311

    def test_an_unstarted_session_has_no_initial_balance(self):
        assert lm.initial_balance([], session_open_ts=MON, minutes=60) is None


class TestAsCandidateLevels:
    def test_every_level_comes_out_in_the_shape_level_detector_uses(self):
        """`{price, type, strength}` -- the same dict `score_level` and
        `_deduplicate_levels` already consume, so these slot into the
        existing candidate list rather than needing a parallel path."""
        candles = [d1(MON, 3290, 3310, 3280, 3300),
                   d1(MON + DAY, 3301, 3305, 3295, 3302)]
        levels = lm.as_candidate_levels(candles, now=MON + DAY + 3600)
        assert levels
        for lvl in levels:
            assert set(lvl) >= {"price", "type", "strength"}
            assert lvl["price"] > 0

    def test_the_types_are_new_ones_not_reused_names(self):
        """A new level reported as `swing_high` would inherit that type's
        fitted score and be invisible in every per-type performance table.
        These get their own names so their edge can be measured separately
        before anyone trusts them."""
        candles = [d1(MON, 3290, 3310, 3280, 3300),
                   d1(MON + DAY, 3301, 3305, 3295, 3302)]
        types = {l["type"] for l in lm.as_candidate_levels(candles, now=MON + DAY + 3600)}
        assert types <= set(lm.LEVEL_TYPES)
        assert not types & {"swing_high", "swing_low", "asia_low", "asia_high"}


class TestVwapAndValueAreaLevels:
    """VWAP and the prior session's value area are the other half of
    section 4.1. They come from the same intraday candles the initial
    balance already needs, so including them costs no extra data."""

    def _m5(self, n=24, base=3300.0):
        return [{"ts": MON + i * 300, "open": base + i * 0.1,
                 "high": base + i * 0.1 + 1, "low": base + i * 0.1 - 1,
                 "close": base + i * 0.1, "tick_volume": 100 + i}
                for i in range(n)]

    def test_session_vwap_becomes_a_level(self):
        levels = lm.as_candidate_levels([], now=MON + 7200,
                                        intraday_candles=self._m5(),
                                        session_open_ts=MON)
        types = {l["type"] for l in levels}
        assert "vwap" in types

    def test_the_value_area_edges_and_point_of_control_become_levels(self):
        levels = lm.as_candidate_levels([], now=MON + 7200,
                                        intraday_candles=self._m5(),
                                        session_open_ts=MON)
        types = {l["type"] for l in levels}
        assert {"poc", "vah", "val"} <= types

    def test_they_are_all_declared_level_types(self):
        levels = lm.as_candidate_levels([], now=MON + 7200,
                                        intraday_candles=self._m5(),
                                        session_open_ts=MON)
        assert {l["type"] for l in levels} <= set(lm.LEVEL_TYPES)

    def test_too_few_candles_to_build_a_profile_adds_nothing_rather_than_zero(self):
        """A profile needs a price range. One flat candle has none, and a
        POC of 0.0 entering the candidate list as a level would be a price
        the market has never traded at."""
        flat = [{"ts": MON, "open": 3300.0, "high": 3300.0, "low": 3300.0,
                 "close": 3300.0}]
        levels = lm.as_candidate_levels([], now=MON + 600,
                                        intraday_candles=flat,
                                        session_open_ts=MON)
        assert not any(l["type"] in ("poc", "vah", "val") for l in levels)


class TestTheSessionAnchor:
    """VWAP, the volume profile and the initial balance all measure from
    one anchor, and which anchor is not arbitrary."""

    def test_it_is_the_most_recent_london_open(self):
        from backend.src.services.reversal_engine import cycle_setup as cs
        from datetime import datetime, timezone
        noon = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
        got = datetime.fromtimestamp(cs.session_anchor(noon), tz=timezone.utc)
        assert (got.hour, got.day) == (7, 9)

    def test_before_the_open_it_rolls_back_to_yesterday(self):
        """At 03:00 the London session has not started. Anchoring forward to
        an open that has not happened would measure a VWAP over no candles
        at all."""
        from backend.src.services.reversal_engine import cycle_setup as cs
        from datetime import datetime, timezone
        early = datetime(2026, 9, 9, 3, 0, tzinfo=timezone.utc).timestamp()
        got = datetime.fromtimestamp(cs.session_anchor(early), tz=timezone.utc)
        assert (got.hour, got.day) == (7, 8)
