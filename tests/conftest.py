"""Shared test setup.

No test may reach the live economic calendar feed.

Until 2026-08-07 nothing isolated the suite from it, so a test run consulted
the real ForexFactory feed and the real wall clock. That makes results depend
on the world: on the morning of Friday 2026-08-07 the same suite went from
2507 passing to 166 failing without a line of code changing, by two separate
routes on the same day --

  * the feed started answering HTTP 429, leaving no events, so
    check_news_blackout() fell through to _hardcoded_fallback(), whose
    "NFP Friday" rule blacks out 12:00-16:00 UTC on the first Friday of the
    month; and
  * once the 429 cleared, the feed correctly reported a live high-impact
    event (Average Hourly Earnings), so the blackout fired for real.

Either way ~160 tests that have nothing to do with news failed on
"News blackout ... (Trading > News)" from the entry path. A suite that only
passes outside a news window, and only while an external host is willing to
serve it, cannot tell you whether your code works.

So: the feed is stubbed out and the blackout defaults to disabled for every
test. Tests that are about the calendar or the blackout override this with
their own monkeypatching -- theirs applies after this fixture, so it wins --
and they supply their own events and settings, which is what makes them
deterministic. Mark a test `@pytest.mark.live_news` if it genuinely needs the
network.
"""
import pytest

from forex_trader.core import news_calendar as _nc


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_news: test deliberately uses the live economic calendar feed "
        "(exempt from the offline stub in tests/conftest.py)",
    )


@pytest.fixture(autouse=True)
def _offline_news_calendar(request, monkeypatch, tmp_path):
    """Keep the calendar offline and the blackout out of the way by default.

    This neutralises the module's *inputs* rather than replacing _fetch_raw or
    get_blackout_settings themselves, so the tests that exist to exercise those
    two functions still get the real ones. They stub urlopen and config.get on
    their own account, after this fixture, and their setup wins.
    """
    if "live_news" in request.keywords:
        return

    # A primed (empty) cache dated far in the future: the real _fetch_raw
    # returns on its first line and never builds a request.
    monkeypatch.setattr(_nc, "_cache_events", [], raising=False)
    monkeypatch.setattr(_nc, "_next_fetch_ts", float("inf"), raising=False)

    # The rest of the module's cross-request state. These are module globals,
    # so without this a failure in one test would change the backoff another
    # test computes, and the on-disk cache would let the developer's own
    # machine answer a test that is supposed to have no data.
    monkeypatch.setattr(_nc, "_fail_streak", 0, raising=False)
    monkeypatch.setattr(_nc, "_validators", {}, raising=False)
    monkeypatch.setattr(_nc, "_disk_loaded", False, raising=False)
    monkeypatch.setattr(
        _nc, "_disk_cache_file",
        lambda: tmp_path / "news_calendar_cache.json",
    )

    # check_news_blackout() short-circuits on `enabled` before it looks at any
    # event. Turning it off through config leaves get_blackout_settings itself
    # real, including its clamping of the padding values. Every other key is
    # delegated, so this is invisible to tests that read unrelated config.
    import forex_trader.config as _cfg
    _real_get = _cfg.get

    def _get(key, default=None):
        if key == "news_blackout_enabled":
            return False
        return _real_get(key, default)

    monkeypatch.setattr(_cfg, "get", _get)
