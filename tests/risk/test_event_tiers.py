"""Per-tier event windows, and the asymmetry that matters.

Section 5.6. `news_calendar.get_blackout_settings()` gives ONE before/after
window for whichever impact threshold is configured, so FOMC and a
middling retail-sales print are treated identically. They are not
identical, and the danger is not symmetric either: the twenty minutes AFTER
a release, when the spread is wide and stops are being harvested, is worse
than the twenty before.

This composes over news_calendar's own events rather than re-fetching or
re-rating them -- the feed, the gold weighting and the impact ranks stay in
one place.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import event_tiers as et


def ev(title, impact="high", mins_until=10.0, currency="USD"):
    return {"title": title, "impact": impact, "currency": currency,
            "mins_until": mins_until}


class TestTiering:
    def test_a_rate_decision_is_the_top_tier(self):
        assert et.tier_of(ev("FOMC Statement")) == 1
        assert et.tier_of(ev("Fed Interest Rate Decision")) == 1

    def test_cpi_and_payrolls_are_the_top_tier(self):
        assert et.tier_of(ev("CPI m/m")) == 1
        assert et.tier_of(ev("Non-Farm Employment Change")) == 1

    def test_a_second_rank_release_is_tier_two(self):
        assert et.tier_of(ev("Retail Sales m/m")) == 2
        assert et.tier_of(ev("PPI m/m")) == 2

    def test_an_unremarkable_high_impact_event_is_tier_three(self):
        assert et.tier_of(ev("Trade Balance")) == 3

    def test_a_low_impact_event_is_never_promoted_by_its_title(self):
        """A title match must not outrank the feed's own rating, or a
        "Fed Member Speaks" footnote becomes an FOMC blackout."""
        assert et.tier_of(ev("Fed Member Speaks", impact="low")) == 3


class TestWindows:
    def test_the_top_tier_gets_the_widest_window(self):
        cfg = et.Config()
        assert cfg.window_for(1)[0] > cfg.window_for(2)[0]
        assert cfg.window_for(2)[0] > cfg.window_for(3)[0]

    def test_the_window_after_is_wider_than_the_window_before(self):
        """The spread is widest and the stop-harvesting worst in the minutes
        AFTER the number, not before it."""
        for tier in (1, 2, 3):
            before, after = et.Config().window_for(tier)
            assert after > before


class TestTheCheck:
    def test_an_imminent_tier_one_event_blocks(self):
        ok, reason = et.check([ev("FOMC Statement", mins_until=20.0)],
                              et.Config())
        assert ok is False
        assert "FOMC" in reason

    def test_the_same_distance_from_a_tier_three_event_does_not_block(self):
        assert et.check([ev("Trade Balance", mins_until=20.0)],
                        et.Config())[0] is True

    def test_an_event_that_has_just_passed_still_blocks(self):
        ok, reason = et.check([ev("CPI m/m", mins_until=-10.0)], et.Config())
        assert ok is False
        assert "since" in reason.lower()

    def test_an_event_long_past_does_not_block(self):
        assert et.check([ev("CPI m/m", mins_until=-600.0)], et.Config())[0] is True

    def test_no_events_allows_trading(self):
        assert et.check([], et.Config()) == (True, "")

    def test_disabled_allows_everything(self):
        assert et.check([ev("FOMC Statement", mins_until=1.0)],
                        et.Config(enabled=False))[0] is True

    def test_the_worst_overlapping_event_is_the_one_reported(self):
        ok, reason = et.check([ev("Trade Balance", mins_until=5.0),
                               ev("FOMC Statement", mins_until=5.0)],
                              et.Config())
        assert ok is False
        assert "FOMC" in reason
