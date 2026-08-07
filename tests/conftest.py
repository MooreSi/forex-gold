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

The same lesson, learned again on 2026-08-07 at 21:08 UTC
-----------------------------------------------------------
Eight minutes after the forex week closed (Fri 21:00 UTC), 147 tests started
failing with "Trading session 'closed' is not active in your Trading Markets
selection". Not one of them was about market hours.

core_db_risk_settings.is_session_allowed() calls dpm_engine.
is_weekly_market_closed() with no way to override the clock, and
core_signal_resolution.resolve() raises on it before reaching anything a test
came to check -- so every test that resolves a signal fails from Friday 21:00
UTC to Sunday 22:00 UTC and passes the rest of the week. The suite was
stubbed against the news feed but not against the calendar week, so the same
trap was still set one function over.

So the week is pinned open by default, exactly like the blackout above. Tests
about weekend behaviour override it themselves -- theirs applies after this
fixture, so it wins -- or take the `live_market_hours` marker to opt out
entirely.
"""
import pytest

import logging
import logging.handlers

from forex_trader.core import dpm_engine as _dpm
from forex_trader.core import news_calendar as _nc


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_news: test deliberately uses the live economic calendar feed "
        "(exempt from the offline stub in tests/conftest.py)",
    )
    config.addinivalue_line(
        "markers",
        "live_market_hours: test deliberately reads the real calendar week "
        "(exempt from the market-open stub in tests/conftest.py)",
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


@pytest.fixture(autouse=True)
def _market_week_open(request, monkeypatch):
    """Pin the forex week open so the suite means the same thing on a Sunday.

    Only the weekly close is neutralised, not dpm_engine.detect_session():
    that one maps hour-of-day onto asian/london/overlap/ny, and all three
    session toggles ship enabled (vantage_risk_settings.session_*_enabled
    DEFAULT 1), so whatever hour the suite runs at is allowed. "closed" is the
    single value that fails, and it comes only from here.

    Patched in two places because core_closed_market_queue binds the function
    by name at import time, so patching the dpm_engine attribute alone would
    leave that module holding the real one.
    """
    if "live_market_hours" in request.keywords:
        return

    monkeypatch.setattr(_dpm, "is_weekly_market_closed", lambda now=None: False)

    from forex_trader.core import core_closed_market_queue as _cmq
    monkeypatch.setattr(_cmq, "is_weekly_market_closed", lambda now=None: False)


@pytest.fixture(autouse=True)
def _never_write_to_the_apps_log():
    """Detach any file handler aimed at the user data directory.

    Importing `run` used to attach a rotating file handler to the ROOT logger
    at module scope, pointed at the LIVE app's forex_trader.log -- and
    tests/test_claim_port.py imports it, so every pytest session wrote into
    the log of whatever app instance was running at the time. On 2026-08-07
    that put five WARNINGs about an EA outage and a failed terminal restart
    into the production log; none of it happened, the durations came from a
    fixture and the exception was injected. A log that invents outages is
    worse than no log, because it is read precisely when something is wrong.
    Two processes were also sharing one TimedRotatingFileHandler, so both
    would try to perform the midnight rename.

    run.setup_logging() is now called from main() rather than at import, which
    fixes that at the source. This is the guard that stops the next one, and
    it runs per-test rather than per-session so a handler attached midway
    through a run is gone again by the next test. Handlers writing anywhere
    else -- a tmp_path, caplog's own -- are left alone.
    """
    from forex_trader.config import USER_DATA_DIR
    target = str(USER_DATA_DIR)

    removed = []
    for name in [None] + list(logging.root.manager.loggerDict):
        logger = logging.getLogger(name) if name else logging.getLogger()
        if not isinstance(logger, logging.Logger):
            continue
        for h in list(logger.handlers):
            path = getattr(h, "baseFilename", None)
            if path and target in str(path):
                logger.removeHandler(h)
                h.close()
                removed.append(str(path))
    if removed:
        # Loud on purpose: something reintroduced the import-time side effect.
        print(f"\n[conftest] detached {len(removed)} handler(s) writing to the "
              f"app's data dir: {sorted(set(removed))}")
    yield
