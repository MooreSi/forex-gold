"""Economic news calendar: feed parsing, gold ranking, and the entry blackout.

The bug that motivated most of this: the ForexFactory feed names its currency
field `country`, and the parser read it as `currency`. Every event therefore
had currency None, nothing ever matched the USD/XAU filter, and the blackout
silently never fired while looking healthy — the feed fetch succeeded, the
event list was non-empty, and is_high_impact_window() just returned False
forever. test_currency_is_read_from_country_field and
test_blackout_fires_inside_window_of_usd_high_impact are the regression tests
for that; they fail against the old `ev.get("currency")` parse.
"""
import pytest

import backend.src.config as _cfg_module
from backend.src.utils import news_calendar as nc

# Captured at import, before any fixture runs. tests/conftest.py's autouse
# _offline_news_calendar replaces config.get with one that forces
# news_blackout_enabled False, so the class at the bottom of this file -- the
# one whose whole subject is whether that key round-trips -- has to put the
# real accessor back. Its docstring invites exactly this: "their setup wins".
_REAL_CFG_GET = _cfg_module.get


# One real week's worth of feed shape, trimmed. Field names and impact casing
# are copied verbatim from the live feed.
_FEED = [
    {"title": "Non-Farm Employment Change", "country": "USD",
     "date": "2026-08-07T08:30:00-04:00", "impact": "High",
     "forecast": "175K", "previous": "147K"},
    {"title": "ISM Manufacturing PMI", "country": "USD",
     "date": "2026-08-03T10:00:00-04:00", "impact": "High",
     "forecast": "49.1", "previous": "49.0"},
    {"title": "Employment Change", "country": "CAD",
     "date": "2026-08-07T08:30:00-04:00", "impact": "High",
     "forecast": "", "previous": "83.1K"},
    {"title": "German Factory Orders m/m", "country": "EUR",
     "date": "2026-08-06T02:00:00-04:00", "impact": "Medium",
     "forecast": "0.5%", "previous": "-1.4%"},
    {"title": "Building Consents m/m", "country": "NZD",
     "date": "2026-08-02T18:45:00-04:00", "impact": "Low",
     "forecast": "", "previous": "-4.0%"},
    {"title": "Bank Holiday", "country": "AUD",
     "date": "2026-08-02T17:00:00-04:00", "impact": "Holiday",
     "forecast": "", "previous": ""},
]

# 2026-08-07T08:30:00-04:00
_NFP_TS = 1786105800.0


@pytest.fixture
def feed(monkeypatch):
    """Serve the fixture feed and pin the blackout config to known defaults."""
    monkeypatch.setattr(nc, "_fetch_raw", lambda: list(_FEED))
    monkeypatch.setattr(nc, "get_blackout_settings", lambda: {
        "enabled": True,
        "impact": "high",
        "impacts": frozenset({"high"}),
        "minutes_before": 30,
        "minutes_after": 30,
    })
    return _FEED


@pytest.fixture
def at_time(monkeypatch):
    """Pin nc's clock. Returns a setter so a test can move time around."""
    def _set(ts):
        monkeypatch.setattr(nc.time, "time", lambda: ts)
    return _set


def _titles(events):
    return [e["title"] for e in events]


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_currency_is_read_from_country_field(feed):
    """The feed has no `currency` key; reading one yields None for every event."""
    events = nc.get_events()
    assert {e["currency"] for e in events} == {"USD", "CAD", "EUR", "NZD", "AUD"}
    assert all(e["currency"] for e in events)


def test_events_are_sorted_by_time_not_feed_order(feed):
    events = nc.get_events()
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    assert events[0]["title"] == "Bank Holiday"       # Aug 2 17:00-04:00
    assert events[-1]["title"] in {"Non-Farm Employment Change", "Employment Change"}


def test_feed_offsets_are_converted_to_utc(feed):
    nfp = next(e for e in nc.get_events() if e["title"] == "Non-Farm Employment Change")
    # 08:30 New York (-04:00) is 12:30 UTC, not 08:30.
    assert nfp["dt"].hour == 12
    assert nfp["dt"].minute == 30
    assert nfp["ts"] == _NFP_TS


def test_unparseable_rows_are_dropped_not_raised(monkeypatch):
    monkeypatch.setattr(nc, "_fetch_raw", lambda: [
        {"title": "No date", "country": "USD", "impact": "High"},
        {"title": "Bad date", "country": "USD", "impact": "High", "date": "not-a-date"},
        _FEED[0],
    ])
    assert _titles(nc.get_events()) == ["Non-Farm Employment Change"]


def test_filters_apply_together(feed):
    events = nc.get_events(impacts={"high"}, currencies={"USD"})
    assert _titles(events) == ["ISM Manufacturing PMI", "Non-Farm Employment Change"]


def test_upcoming_only_excludes_past_events(feed, at_time):
    at_time(_NFP_TS - 60)
    upcoming = nc.get_events(impacts={"high"}, upcoming_only=True)
    # ISM (Aug 3) is behind us; NFP and the CAD print are still ahead.
    assert "ISM Manufacturing PMI" not in _titles(upcoming)
    assert "Non-Farm Employment Change" in _titles(upcoming)


# ── Gold relevance ranking ────────────────────────────────────────────────────

def test_usd_high_impact_outranks_same_impact_foreign_event(feed):
    events = {e["title"]: e["score"] for e in nc.get_events()}
    # Identical "High" rating, but a CAD jobs print is not a gold event.
    assert events["Non-Farm Employment Change"] > events["Employment Change"]


def test_keyword_boost_separates_two_usd_high_impact_events(feed):
    events = {e["title"]: e["score"] for e in nc.get_events()}
    assert events["Non-Farm Employment Change"] > events["ISM Manufacturing PMI"]


def test_holiday_scores_zero(feed):
    holiday = next(e for e in nc.get_events() if e["impact"] == "holiday")
    assert holiday["score"] == 0.0


# ── Blackout window ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("offset_min", [-29, 0, 29])
def test_blackout_fires_inside_window_of_usd_high_impact(feed, at_time, offset_min):
    at_time(_NFP_TS + offset_min * 60)
    assert nc.is_high_impact_window() is True
    assert nc.get_current_event()["title"] == "Non-Farm Employment Change"


@pytest.mark.parametrize("offset_min", [-31, 31])
def test_blackout_clears_outside_window(feed, at_time, offset_min):
    at_time(_NFP_TS + offset_min * 60)
    assert nc.is_high_impact_window() is False
    assert nc.get_current_event() is None


def test_foreign_high_impact_event_does_not_trigger_blackout(monkeypatch, at_time):
    """A CAD print at the same minute as NFP must not, alone, stop gold trading."""
    cad_only = [e for e in _FEED if e["country"] == "CAD"]
    monkeypatch.setattr(nc, "_fetch_raw", lambda: cad_only)
    monkeypatch.setattr(nc, "get_blackout_settings", lambda: {
        "enabled": True, "impact": "high", "impacts": frozenset({"high"}),
        "minutes_before": 30, "minutes_after": 30,
    })
    at_time(_NFP_TS)
    assert nc.is_high_impact_window() is False


def test_medium_impact_only_blacks_out_when_configured(monkeypatch, at_time):
    eur_ts = 1785996000.0  # 2026-08-06T02:00:00-04:00
    monkeypatch.setattr(nc, "_fetch_raw", lambda: list(_FEED))
    monkeypatch.setattr(nc, "_currency_of", lambda raw: "USD")  # make it gold-relevant
    at_time(eur_ts)

    def _settings(impacts, name):
        return lambda: {"enabled": True, "impact": name, "impacts": frozenset(impacts),
                        "minutes_before": 30, "minutes_after": 30}

    monkeypatch.setattr(nc, "get_blackout_settings", _settings({"high"}, "high"))
    assert nc.is_high_impact_window() is False

    monkeypatch.setattr(nc, "get_blackout_settings",
                        _settings({"high", "medium"}, "high_medium"))
    assert nc.is_high_impact_window() is True


def test_disabled_blackout_never_fires_even_mid_event(monkeypatch, feed, at_time):
    monkeypatch.setattr(nc, "get_blackout_settings", lambda: {
        "enabled": False, "impact": "high", "impacts": frozenset({"high"}),
        "minutes_before": 30, "minutes_after": 30,
    })
    at_time(_NFP_TS)
    assert nc.is_high_impact_window() is False


def test_overlapping_windows_return_the_one_ending_last(monkeypatch, at_time):
    """Caller must wait out the longest window, not the first one matched."""
    overlapping = [
        {"title": "Earlier", "country": "USD", "date": "2026-08-07T08:30:00-04:00",
         "impact": "High", "forecast": "", "previous": ""},
        {"title": "Later", "country": "USD", "date": "2026-08-07T08:45:00-04:00",
         "impact": "High", "forecast": "", "previous": ""},
    ]
    monkeypatch.setattr(nc, "_fetch_raw", lambda: overlapping)
    monkeypatch.setattr(nc, "get_blackout_settings", lambda: {
        "enabled": True, "impact": "high", "impacts": frozenset({"high"}),
        "minutes_before": 30, "minutes_after": 30,
    })
    at_time(_NFP_TS)
    assert nc.get_current_event()["title"] == "Later"


def test_mins_to_event_is_negative_after_the_event(feed, at_time):
    at_time(_NFP_TS + 10 * 60)
    ev = nc.get_current_event()
    assert ev["mins_to_event"] == -10.0
    assert ev["mins_remaining"] == 20.0


# ── ML feature ────────────────────────────────────────────────────────────────

def test_proximity_norm_scales_with_distance_to_next_event(feed, at_time):
    """The norm tracks the distance to the next high-impact event.

    refresh_now() between clock moves because get_news_proximity_norm() is a
    pure cache read on this branch -- it never fetches inline, which is what
    keeps ~10s of blocking urllib off the engine's every cycle (backend review
    2026-08-08 #5, pinned by tests/utils/test_news_calendar_offload.py).
    Upstream computes it on read instead, so its version of this test needed no
    refresh; the assertion below is unchanged from upstream's.
    """
    at_time(_NFP_TS - 120 * 60)
    nc.refresh_now()
    assert nc.get_news_proximity_norm() == 1.0
    at_time(_NFP_TS - 60 * 60)
    nc.refresh_now()
    assert nc.get_news_proximity_norm() == 0.5
    at_time(_NFP_TS - 6 * 60)
    nc.refresh_now()
    assert nc.get_news_proximity_norm() == 0.05


def test_proximity_norm_is_safe_when_no_events_remain(feed, at_time):
    at_time(_NFP_TS + 86400)
    nc.refresh_now()   # pure cache read on this branch -- see the test above
    assert nc.get_news_proximity_norm() == 1.0


def test_proximity_norm_ignores_non_gold_currencies(monkeypatch, at_time):
    """A CAD event 10 min out must not read as imminent risk for XAUUSD."""
    cad_only = [e for e in _FEED if e["country"] == "CAD"]
    monkeypatch.setattr(nc, "_fetch_raw", lambda: cad_only)
    at_time(_NFP_TS - 10 * 60)
    nc.refresh_now()   # pure cache read on this branch -- see the test above
    assert nc.get_news_proximity_norm() == 1.0


# ── Fetch resilience ──────────────────────────────────────────────────────────

def test_failed_fetch_serves_last_good_payload_and_backs_off(monkeypatch):
    nc._cache_events = None
    nc._next_fetch_ts = 0.0
    calls = []

    def _boom(*a, **kw):
        calls.append(1)
        raise OSError("429 Too Many Requests")

    monkeypatch.setattr(nc.time, "time", lambda: 1_000_000.0)
    monkeypatch.setattr("urllib.request.urlopen", _boom)

    # Cold cache + failing feed: one attempt, then backoff, not one per call.
    assert nc._fetch_raw() == []
    assert nc._fetch_raw() == []
    assert len(calls) == 1
    assert nc._next_fetch_ts == 1_000_000.0 + nc._RETRY_AFTER

    # Once a payload has been cached, a later failure keeps serving it.
    nc._cache_events = list(_FEED)
    nc._next_fetch_ts = 0.0
    assert len(nc._fetch_raw()) == len(_FEED)
    assert len(calls) == 2

    nc._cache_events = None
    nc._next_fetch_ts = 0.0


def test_blackout_falls_back_to_schedule_when_feed_never_loaded(monkeypatch):
    """With no feed at all, FOMC day must still suppress entries."""
    monkeypatch.setattr(nc, "_fetch_raw", lambda: [])
    monkeypatch.setattr(nc, "get_blackout_settings", lambda: {
        "enabled": True, "impact": "high", "impacts": frozenset({"high"}),
        "minutes_before": 30, "minutes_after": 30,
    })
    # 2026-09-17 is an FOMC date; 19:00 UTC is inside the 12:00-22:00 suppression.
    monkeypatch.setattr(nc, "_hardcoded_fallback", lambda now: True)
    assert nc.is_high_impact_window() is True


# ── Settings validation ───────────────────────────────────────────────────────

def test_unknown_impact_name_falls_back_to_high_only(monkeypatch):
    monkeypatch.setattr(nc, "_DEF_BLACKOUT_IMPACT", "high")
    import backend.src.config as cfg
    monkeypatch.setattr(cfg, "get", lambda k, d=None: {
        "news_blackout_enabled": True,
        "news_blackout_impact": "everything",
        "news_blackout_minutes_before": 30,
        "news_blackout_minutes_after": 30,
    }.get(k, d))
    s = nc.get_blackout_settings()
    assert s["impact"] == "high"
    assert s["impacts"] == frozenset({"high"})


def test_absurd_padding_is_clamped_not_honoured(monkeypatch):
    import backend.src.config as cfg
    monkeypatch.setattr(cfg, "get", lambda k, d=None: {
        "news_blackout_enabled": True,
        "news_blackout_impact": "high",
        "news_blackout_minutes_before": 99999,
        "news_blackout_minutes_after": -5,
    }.get(k, d))
    s = nc.get_blackout_settings()
    assert s["minutes_before"] == 240
    assert s["minutes_after"] == 0



# ── The settings actually survive a save ──────────────────────────────────────
#
# Every test above stubs `cfg.get`, which is precisely how this went unnoticed:
# `config.load()` rebuilds its dict from a literal list of named keys, the four
# blackout keys were not on it, and `save_to_yaml()` reloads at the end. So
# Settings > News wrote all four to config.yaml correctly and then every reader
# got the hardcoded defaults back -- including `enabled`, whose default is True.
# The owner had switched the blackout off in the UI and it had been on ever
# since (found 2026-09-04, alongside the same bug in `auto_login_enabled`).


class TestTheBlackoutSettingsRoundTripThroughConfig:
    @pytest.fixture
    def isolated_config(self, monkeypatch, tmp_path):
        """A real config.yaml in a temp dir, reading through the real
        config.get, with the module cache restored afterwards."""
        cfg_module = _cfg_module

        before = dict(cfg_module._cfg)
        monkeypatch.setattr(cfg_module, "CONFIG_FILE", tmp_path / "config.yaml")
        monkeypatch.setattr(cfg_module, "get", _REAL_CFG_GET)
        yield cfg_module
        cfg_module._cfg = before

    def _save(self, values):
        from backend.src.controllers import settings_controller as controller
        controller.save_config(values)

    def test_switching_the_blackout_off_sticks(self, isolated_config):
        """The one that was broken: `enabled` defaults to True, so a dropped
        key left the blackout on no matter what the page said."""
        self._save({"news_blackout_enabled": False})

        assert nc.get_blackout_settings()["enabled"] is False

    def test_switching_it_back_on_sticks_too(self, isolated_config):
        """Negative control -- False must not be hardcoded in the fix."""
        self._save({"news_blackout_enabled": False})
        self._save({"news_blackout_enabled": True})

        assert nc.get_blackout_settings()["enabled"] is True

    def test_impact_and_padding_survive(self, isolated_config):
        """All four keys, at values distinguishable from every default.
        "high_medium" is the only other impact the page offers; anything else
        is rejected upstream by _IMPACT_SETS, as the test above pins."""
        self._save({
            "news_blackout_enabled": True,
            "news_blackout_impact": "high_medium",
            "news_blackout_minutes_before": 45,
            "news_blackout_minutes_after": 15,
        })

        s = nc.get_blackout_settings()

        assert s["impact"] == "high_medium"
        assert s["impacts"] == frozenset({"high", "medium"})
        assert s["minutes_before"] == 45
        assert s["minutes_after"] == 15

    def test_a_restart_still_sees_them(self, isolated_config):
        """A fresh load(), not just the reload save_to_yaml() does."""
        self._save({"news_blackout_enabled": False,
                    "news_blackout_minutes_before": 5})
        isolated_config.load()

        s = nc.get_blackout_settings()

        assert s["enabled"] is False
        assert s["minutes_before"] == 5

    def test_clamping_still_applies_to_saved_values(self, isolated_config):
        """The round trip must not become a way past the 0..240 clamp."""
        self._save({"news_blackout_minutes_before": 99999,
                    "news_blackout_minutes_after": -5})

        s = nc.get_blackout_settings()

        assert s["minutes_before"] == 240
        assert s["minutes_after"] == 0

    def test_an_untouched_install_keeps_the_blackout_on(self, isolated_config):
        """Nothing saved: the safe default still stands."""
        assert nc.get_blackout_settings()["enabled"] is True

# ── check_news_blackout: the (allowed, reason) gate used by the entry paths ───

def test_gate_allows_when_no_event_is_active(feed, at_time):
    at_time(_NFP_TS - 60 * 60)
    allowed, reason = nc.check_news_blackout()
    assert allowed is True
    assert reason == ""


def test_gate_blocks_and_names_the_event_and_the_wait(feed, at_time):
    at_time(_NFP_TS - 10 * 60)
    allowed, reason = nc.check_news_blackout()
    assert allowed is False
    # The reason reaches the user via skip_reason strings and Telegram alerts,
    # so it has to say what and how long, not just "blocked".
    assert "Non-Farm Employment Change" in reason
    assert "USD" in reason
    assert "40 min" in reason


def test_gate_allows_when_blackout_is_switched_off(monkeypatch, feed, at_time):
    monkeypatch.setattr(nc, "get_blackout_settings", lambda: {
        "enabled": False, "impact": "high", "impacts": frozenset({"high"}),
        "minutes_before": 30, "minutes_after": 30,
    })
    at_time(_NFP_TS)
    assert nc.check_news_blackout() == (True, "")


def test_gate_falls_back_to_schedule_only_when_feed_never_loaded(monkeypatch):
    monkeypatch.setattr(nc, "_fetch_raw", lambda: [])
    monkeypatch.setattr(nc, "get_blackout_settings", lambda: {
        "enabled": True, "impact": "high", "impacts": frozenset({"high"}),
        "minutes_before": 30, "minutes_after": 30,
    })
    monkeypatch.setattr(nc, "_hardcoded_fallback", lambda now: True)
    allowed, reason = nc.check_news_blackout()
    assert allowed is False
    assert "calendar unavailable" in reason


def test_gate_does_not_block_on_a_healthy_quiet_feed(monkeypatch, feed, at_time):
    """A loaded feed with no active window must not reach the fallback."""
    monkeypatch.setattr(nc, "_hardcoded_fallback",
                        lambda now: pytest.fail("fallback reached with a live feed"))
    at_time(_NFP_TS - 6 * 3600)
    assert nc.check_news_blackout() == (True, "")
