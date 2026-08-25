"""Canned news in debug mode (stage2 phase5/020).

Both news fetch sites — `utils/news_calendar` and `test_signal/news_filter` —
must answer from canned data in debug with the network untouched, and must
exercise the proximity code path (an event ~2h out) rather than skip it. Debug
off: the real source is consulted exactly as before (negative controls).

**Rewritten by the 2026-08-25 upstream merge, and the reason matters.** These
tests used to patch `news_calendar._from_mt5` / `_from_finnhub` /
`_from_forexfactory` and `news_filter._CACHE` / `_fetch_calendar`. Upstream
deleted every one of those seams (9e8172e): `_from_mt5` called a bridge method
that does not exist, Finnhub's calendar is a premium endpoint, and
`_from_forexfactory` read the feed's currency field as `currency` when the feed
names it `country` — which is precisely why the blackout never fired on live
data. `news_filter` is now a thin delegate to `news_calendar`.

So the seams changed; the guarantees did not. Each test below asserts the same
thing it always did, against the seam that exists now: `get_events` is the one
source, and `_is_debug` short-circuits ahead of it.

No test here can reach the network: the only source is patched in every case.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.src.services.test_signal import news_filter
from backend.src.utils import news_calendar


def test_canned_minutes_in_debug():
    """Debug returns the canned proximity without consulting the feed."""
    with patch.object(news_calendar, "_is_debug", return_value=True), \
         patch.object(news_calendar, "get_events") as events_src:
        mins = news_calendar._fetch_next_event_minutes()
    assert mins == news_calendar._DEBUG_CANNED_MINUTES
    assert 60.0 <= mins <= 240.0, "the canned event must be near enough to exercise proximity"
    events_src.assert_not_called()


def test_real_source_consulted_when_debug_off():
    """Negative control: with debug off the real source runs."""
    import time
    ts = time.time() + 42.0 * 60.0
    with patch.object(news_calendar, "_is_debug", return_value=False), \
         patch.object(news_calendar, "get_events", return_value=[{"ts": ts}]) as events_src:
        mins = news_calendar._fetch_next_event_minutes()
    events_src.assert_called_once()
    assert mins == __import__("pytest").approx(42.0, abs=0.5)


def test_no_upcoming_events_reads_as_unknown():
    """An empty calendar is "unknown", which the caller maps to norm 1.0 --
    the long-standing "unclear calendar -> trade through" fallback (Q004)."""
    with patch.object(news_calendar, "_is_debug", return_value=False), \
         patch.object(news_calendar, "get_events", return_value=[]):
        assert news_calendar._fetch_next_event_minutes() is None


def test_a_failing_source_is_not_fatal():
    """A raising feed reads as unknown rather than propagating into the
    signal path that calls this every cycle."""
    with patch.object(news_calendar, "_is_debug", return_value=False), \
         patch.object(news_calendar, "get_events", side_effect=RuntimeError("feed down")):
        assert news_calendar._fetch_next_event_minutes() is None


def test_news_filter_delegates_to_the_one_calendar():
    """news_filter must not hold a second feed, cache or parser of its own --
    carrying two was what let the currency-key bug hide in one of them."""
    # The delegates bind news_calendar's functions at import time, so the
    # seam to assert on is the bound alias -- that binding IS the delegation.
    with patch.object(news_filter, "_get_current_event", return_value={"title": "FOMC"}) as ce:
        assert news_filter.get_current_event() == {"title": "FOMC"}
    ce.assert_called_once()
    with patch.object(news_filter, "_is_high_impact_window", return_value=True) as hw:
        assert news_filter.is_high_impact_window() is True
    hw.assert_called_once()
    # And the aliases really are news_calendar's own functions, not copies.
    assert news_filter._get_current_event is news_calendar.get_current_event
    assert news_filter._is_high_impact_window is news_calendar.is_high_impact_window
    assert not hasattr(news_filter, "_CACHE"), "news_filter must keep no cache of its own"
