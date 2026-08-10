"""News calendar: never block the live path, and cache "no event" too.

Backend review 2026-08-08, #5: get_news_proximity_norm did up to ~10s of
blocking urllib on the event loop from every engine cycle, and because the
cache guard was `_cache_next_mins is not None`, the most common result (None =
no upcoming event) was never cached and re-fetched every single call.

These pin the fixed behaviour: the getter reads cache only (never fetches
inline), a None result IS cached, and the decision math is unchanged.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from backend.src.utils import news_calendar as nc


@pytest.fixture(autouse=True)
def _reset_cache():
    nc.stop()
    with nc._lock:
        nc._cache_next_mins = None
        nc._cache_ts = 0.0
    yield
    nc.stop()


def test_read_returns_safe_default_before_any_fetch():
    # No fetch has happened; the getter must return the safe 1.0, not block.
    assert nc.get_news_proximity_norm(120.0) == 1.0


def test_getter_never_fetches_inline_and_starts_no_thread(monkeypatch):
    """The live path must read cache only — never fetch, never spawn a thread."""
    fetch = Mock(return_value=30.0)
    started = Mock()
    monkeypatch.setattr(nc, "_fetch_next_event_minutes", fetch)
    monkeypatch.setattr(nc, "ensure_started", started)
    nc.get_news_proximity_norm(120.0)
    fetch.assert_not_called()
    started.assert_not_called()  # pure cache read — safe from tests and hot paths


def test_refresh_caches_a_none_result(monkeypatch):
    """No-event (None) is a real answer and must be cached, not re-fetched."""
    fetch = Mock(return_value=None)
    monkeypatch.setattr(nc, "_fetch_next_event_minutes", fetch)

    nc.refresh_now()
    assert nc.get_news_proximity_norm(120.0) == 1.0  # None -> safe
    nc.get_news_proximity_norm(120.0)
    assert fetch.call_count == 1  # the second read did NOT refetch


def test_refresh_with_event_sets_the_expected_norm(monkeypatch):
    monkeypatch.setattr(nc, "_fetch_next_event_minutes", lambda: 30.0)
    nc.refresh_now()
    # 30 min into a 120 min window -> 0.25, same formula as before.
    assert nc.get_news_proximity_norm(120.0) == 0.25


def test_decision_math_unchanged():
    """Characterization + negative control on the pure conversion."""
    assert nc._mins_to_norm(None) == 1.0
    assert nc._mins_to_norm(0.0) == 0.0
    assert nc._mins_to_norm(60.0, 120.0) == 0.5
    assert nc._mins_to_norm(60.0, 120.0) != 0.6  # control: the formula matters


def test_refresh_survives_a_fetch_that_raises(monkeypatch):
    """A broken source must not propagate; cache stays safe."""
    monkeypatch.setattr(nc, "_fetch_next_event_minutes", Mock(side_effect=RuntimeError("boom")))
    nc.refresh_now()  # must not raise
    assert nc.get_news_proximity_norm(120.0) == 1.0
