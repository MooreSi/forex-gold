"""How the calendar talks to the feed: caching, backoff, and staying polite.

The feed answers HTTP 429 under repeated calls -- the module docstring's "no
rate limit" is wrong, confirmed live 2026-08-07. Three things made that worse
than it needed to be: the payload lived only in memory, so every process start
re-fetched (and this app restarts on activation, self-update and self-healing);
a failure always retried in a flat 5 minutes, so a rate-limited client kept
asking at the same rate; and nothing ever sent a conditional request, so an
unchanged weekly publish was re-downloaded in full.
"""
import json
import time
import urllib.error

import pytest

from backend.src.utils import news_calendar as nc


_FEED = [
    {"title": "Non-Farm Employment Change", "country": "USD",
     "date": "2026-08-07T08:30:00-04:00", "impact": "High",
     "forecast": "175K", "previous": "147K"},
]


class _Resp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, body, headers=None):
        self._body = body.encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cold(monkeypatch):
    """A cold in-memory cache with a fixed clock."""
    monkeypatch.setattr(nc, "_cache_events", None)
    monkeypatch.setattr(nc, "_next_fetch_ts", 0.0)
    monkeypatch.setattr(nc, "_fail_streak", 0)
    monkeypatch.setattr(nc, "_validators", {})
    monkeypatch.setattr(nc, "_disk_loaded", True)   # off unless a test opts in
    monkeypatch.setattr(nc.time, "time", lambda: 1_000_000.0)


# ── Backoff ───────────────────────────────────────────────────────────────────

def test_repeated_failures_back_off_instead_of_retrying_at_a_fixed_rate(cold, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("429")))

    delays = []
    for _ in range(4):
        nc._next_fetch_ts = 0.0          # pretend the backoff elapsed
        nc._fetch_raw()
        delays.append(nc._next_fetch_ts - 1_000_000.0)

    assert delays[0] == nc._RETRY_AFTER, "first failure must retry as it always did"
    assert delays == sorted(delays), "each further failure must wait longer"
    assert delays[-1] > delays[0], "a feed that keeps refusing must be left alone"
    assert max(delays) <= nc._RETRY_MAX


def test_backoff_resets_once_the_feed_answers(cold, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("429")))
    for _ in range(3):
        nc._next_fetch_ts = 0.0
        nc._fetch_raw()
    assert nc._fail_streak == 3

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: _Resp(json.dumps(_FEED)))
    nc._next_fetch_ts = 0.0
    assert len(nc._fetch_raw()) == 1
    assert nc._fail_streak == 0


def test_retry_after_header_is_honoured_over_our_own_guess(cold, monkeypatch):
    err = urllib.error.HTTPError(nc._FEED_URL, 429, "Too Many Requests",
                                 {"Retry-After": "900"}, None)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(err))
    nc._fetch_raw()
    assert nc._next_fetch_ts == 1_000_000.0 + 900.0


def test_absurd_retry_after_is_clamped(cold, monkeypatch):
    """Being blind to news is a trading risk — a week-long Retry-After is not
    accepted at face value."""
    err = urllib.error.HTTPError(nc._FEED_URL, 429, "Too Many Requests",
                                 {"Retry-After": "604800"}, None)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(err))
    nc._fetch_raw()
    assert nc._next_fetch_ts == 1_000_000.0 + nc._RETRY_MAX


# ── Conditional requests ──────────────────────────────────────────────────────

def test_validators_are_replayed_as_a_conditional_request(cold, monkeypatch):
    sent = {}

    def _first(req, *a, **kw):
        sent["first"] = dict(req.headers)
        return _Resp(json.dumps(_FEED), {"ETag": 'W/"abc"',
                                         "Last-Modified": "Fri, 07 Aug 2026 09:00:00 GMT"})

    monkeypatch.setattr("urllib.request.urlopen", _first)
    nc._fetch_raw()
    assert "If-none-match" not in sent["first"], "nothing to be conditional about yet"

    def _second(req, *a, **kw):
        sent["second"] = dict(req.headers)
        return _Resp(json.dumps(_FEED))

    monkeypatch.setattr("urllib.request.urlopen", _second)
    nc._next_fetch_ts = 0.0
    nc._fetch_raw()
    # urllib title-cases header names.
    assert sent["second"].get("If-none-match") == 'W/"abc"'
    assert sent["second"].get("If-modified-since") == "Fri, 07 Aug 2026 09:00:00 GMT"


def test_304_is_a_success_not_a_failure(cold, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: _Resp(json.dumps(_FEED), {"ETag": '"v1"'}))
    nc._fetch_raw()

    err = urllib.error.HTTPError(nc._FEED_URL, 304, "Not Modified", {}, None)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(err))
    nc._next_fetch_ts = 0.0
    events = nc._fetch_raw()

    assert len(events) == 1, "the cached payload is still the answer"
    assert nc._fail_streak == 0, "unchanged is not a failure"
    assert nc._next_fetch_ts == 1_000_000.0 + nc._CACHE_TTL, "full TTL, not a retry delay"


# ── Disk cache ────────────────────────────────────────────────────────────────

def test_payload_survives_a_restart_without_refetching(cold, monkeypatch, tmp_path):
    """The restart case: activation, self-update and the self-healer all bring
    the process back, and each one used to cost a fresh request."""
    cache_file = tmp_path / "news_calendar_cache.json"
    monkeypatch.setattr(nc, "_disk_cache_file", lambda: cache_file)

    calls = []

    def _once(*a, **kw):
        calls.append(1)
        return _Resp(json.dumps(_FEED), {"ETag": '"v1"'})

    monkeypatch.setattr("urllib.request.urlopen", _once)
    nc._fetch_raw()
    assert len(calls) == 1
    assert cache_file.exists(), "a successful fetch must be written to disk"

    # A brand new process: memory empty, disk intact, TTL not yet elapsed.
    monkeypatch.setattr(nc, "_cache_events", None)
    monkeypatch.setattr(nc, "_next_fetch_ts", 0.0)
    monkeypatch.setattr(nc, "_disk_loaded", False)

    events = nc._fetch_raw()
    assert len(events) == 1, "the restart must be served from disk"
    assert len(calls) == 1, "and must not have gone back to the feed"


def test_expired_disk_cache_still_serves_while_it_refreshes(cold, monkeypatch, tmp_path):
    """Stale events beat no events: going blind mid-week is the worse failure."""
    cache_file = tmp_path / "news_calendar_cache.json"
    cache_file.write_text(json.dumps({
        "fetched_at": 1_000_000.0 - (nc._CACHE_TTL * 10),   # long expired
        "validators": {},
        "events":     _FEED,
    }), encoding="utf-8")
    monkeypatch.setattr(nc, "_disk_cache_file", lambda: cache_file)
    monkeypatch.setattr(nc, "_disk_loaded", False)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("429")))

    events = nc._fetch_raw()
    assert len(events) == 1, "expired is still better than blind"


def test_a_corrupt_disk_cache_is_ignored_not_fatal(cold, monkeypatch, tmp_path):
    cache_file = tmp_path / "news_calendar_cache.json"
    cache_file.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setattr(nc, "_disk_cache_file", lambda: cache_file)
    monkeypatch.setattr(nc, "_disk_loaded", False)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: _Resp(json.dumps(_FEED)))

    assert len(nc._fetch_raw()) == 1
