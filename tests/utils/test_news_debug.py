"""Canned news in debug mode (stage2 phase5/020).

Both news fetch sites — utils/news_calendar and test_signal/news_filter —
must answer from canned data in debug with the network untouched, and
must exercise the proximity code path (an event ~2h out), not skip it.
Debug off: the real sources are consulted exactly as before (negative
controls).

No test here can reach the network: every transport is patched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend.src.services.test_signal import news_filter
from backend.src.utils import news_calendar


def test_canned_minutes_in_debug():
    with patch.object(news_calendar, "_is_debug", return_value=True), \
         patch.object(news_calendar, "_from_mt5") as mt5_src, \
         patch.object(news_calendar, "_from_finnhub") as finnhub_src, \
         patch.object(news_calendar, "_from_forexfactory") as ff_src:
        mins = news_calendar._fetch_next_event_minutes()
    assert mins == news_calendar._DEBUG_CANNED_MINUTES
    assert 60.0 <= mins <= 240.0, "the canned event must be near enough to exercise proximity"
    mt5_src.assert_not_called()
    finnhub_src.assert_not_called()
    ff_src.assert_not_called()


def test_real_sources_consulted_when_debug_off():
    """Negative control: with debug off the source chain runs."""
    with patch.object(news_calendar, "_is_debug", return_value=False), \
         patch.object(news_calendar, "_from_mt5", return_value=None) as mt5_src, \
         patch.object(news_calendar, "_from_finnhub", return_value=None) as finnhub_src, \
         patch.object(news_calendar, "_from_forexfactory", return_value=42.0) as ff_src:
        mins = news_calendar._fetch_next_event_minutes()
    assert mins == 42.0
    mt5_src.assert_called_once()
    finnhub_src.assert_called_once()
    ff_src.assert_called_once()


def test_canned_calendar_in_debug(monkeypatch):
    monkeypatch.setattr(news_filter, "_CACHE", None)
    monkeypatch.setattr(news_filter, "_CACHE_TS", 0.0)
    with patch.object(news_filter, "_is_debug", return_value=True), \
         patch.object(news_filter.urllib.request, "urlopen") as urlopen:
        events = news_filter._fetch_calendar()
    urlopen.assert_not_called()
    assert len(events) == 1
    ev = events[0]
    assert ev["impact"] == "High" and ev["currency"] == "USD"
    assert ev["date"], "the canned event carries a parseable ISO date"


def test_network_calendar_when_debug_off(monkeypatch):
    """Negative control: debug off hits the (patched) transport."""
    monkeypatch.setattr(news_filter, "_CACHE", None)
    monkeypatch.setattr(news_filter, "_CACHE_TS", 0.0)
    resp = MagicMock()
    resp.read.return_value = b"[]"
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    with patch.object(news_filter, "_is_debug", return_value=False), \
         patch.object(news_filter.urllib.request, "urlopen", return_value=resp) as urlopen:
        events = news_filter._fetch_calendar()
    urlopen.assert_called_once()
    assert events == []
