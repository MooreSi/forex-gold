"""
Economic news calendar — provides "minutes to next high-impact event" as an ML feature.

Sources (in priority order):
  1. MT5 bridge calendar query (no external dependency, most reliable)
  2. Finnhub API (free tier, requires FINNHUB_API_KEY in config.yaml)
  3. ForexFactory scrape (fallback, no key needed but fragile)

The primary consumer is the signal ML engines, which use news_proximity_norm:
  0.0 = high-impact event imminent (< 5 min) — very risky to enter
  0.5 = event in ~60 min — moderate caution
  1.0 = no event in the next 120+ min — safe window

Cache TTL: 10 minutes (events don't change that quickly).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

_log = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
# The cache is refreshed by a background daemon thread, NEVER inline from a
# caller. Two bugs are fixed by this (backend review 2026-08-08, #5): the fetch
# did up to ~10s of blocking urllib ON THE EVENT LOOP, and because the old guard
# was `_cache_next_mins is not None`, a None result (the common "no upcoming
# event" case) was never cached and re-fetched every single call. Now the getter
# only ever reads the cache, and None is a cached value like any other.
_lock = threading.Lock()
_cache_ts:           float = 0.0
_cache_next_mins:    Optional[float] = None   # minutes to next high-impact event; None = none/unknown
_CACHE_TTL:          float = 600.0            # refresh interval, 10 minutes

_refresh_thread:     Optional[threading.Thread] = None
_wake  = threading.Event()   # nudge the refresher to fetch now (see invalidate_cache)
_stop  = threading.Event()

# Currencies that affect XAUUSD meaningfully
_IMPACT_CURRENCIES = {"USD", "XAU", "US", "EUR", "GBP"}
_HIGH_IMPACT       = {"high", "3", "red"}     # Finnhub uses "3"; FF uses "red"/"high"


def _mins_to_norm(minutes: Optional[float], window: float = 120.0) -> float:
    """Convert minutes-to-event to a [0,1] norm. 0=imminent, 1=far/safe."""
    if minutes is None:
        return 1.0
    return round(min(1.0, max(0.0, float(minutes) / window)), 4)


def refresh_now() -> Optional[float]:
    """Fetch once and update the cache. BLOCKS — only the refresher thread (and
    tests) call this; never a live signal path. A failing fetch is swallowed and
    leaves the cache at None (safe), matching the previous error behaviour."""
    global _cache_ts, _cache_next_mins
    try:
        mins = _fetch_next_event_minutes()
    except Exception as e:  # a source raising must never take down the caller
        _log.debug("[NewsCalendar] refresh error: %s", e)
        mins = None
    with _lock:
        _cache_next_mins = mins
        _cache_ts = time.time()
    return mins


def _refresh_loop() -> None:
    while not _stop.is_set():
        refresh_now()
        _wake.wait(_CACHE_TTL)   # sleep until the interval, or until nudged
        _wake.clear()


def ensure_started() -> None:
    """Start the background refresher once (idempotent). Called lazily by the
    getter so news data stays fresh without any caller having to remember to
    start it; also safe to call explicitly at app startup."""
    global _refresh_thread
    with _lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _stop.clear()
        _refresh_thread = threading.Thread(
            target=_refresh_loop, name="news-calendar-refresh", daemon=True
        )
        _refresh_thread.start()


def stop() -> None:
    """Stop the background refresher (tests, shutdown)."""
    _stop.set()
    _wake.set()


def get_news_proximity_norm(window_minutes: float = 120.0) -> float:
    """
    Return news_proximity_norm [0,1] from the background-refreshed cache.

    NEVER blocks and never fetches inline — the live signal paths call this every
    cycle. It is a pure cache read: it does not start threads or touch the
    network, so it is safe to call from anywhere (and from tests). Returns 1.0
    (safe) until the background refresher — started once at app boot via
    ensure_started() — has populated the cache, which matches the long-standing
    "unclear calendar -> trade through" fallback.
    """
    with _lock:
        mins = _cache_next_mins
    return _mins_to_norm(mins, window_minutes)


def _fetch_next_event_minutes() -> Optional[float]:
    """Try each source in order, return minutes to next high-impact event or None."""
    mins = _from_mt5()
    if mins is not None:
        return mins

    mins = _from_finnhub()
    if mins is not None:
        return mins

    mins = _from_forexfactory()
    if mins is not None:
        return mins

    _log.debug("[NewsCalendar] All sources failed — assuming no imminent event")
    return None


# ── Source 1: MT5 Bridge ──────────────────────────────────────────────────────

def _from_mt5() -> Optional[float]:
    """Query MT5 economic calendar via the bridge. Returns minutes or None."""
    try:
        from backend.src.services.broker import mt5_client as bridge
        if not bridge.is_connected():
            return None

        import datetime as _dt
        now_ts  = time.time()
        end_ts  = now_ts + 7200  # look 2 hours ahead
        now_dt  = _dt.datetime.fromtimestamp(now_ts)
        end_dt  = _dt.datetime.fromtimestamp(end_ts)

        # MT5 Python API: MetaTrader5.calendar_query(from, to)
        import MetaTrader5 as _mt5
        events = _mt5.calendar_query(now_dt, end_dt) or []

        min_delta = None
        for ev in events:
            # event is a namedtuple; currency and importance accessible as attributes
            currency   = getattr(ev, "currency", "") or ""
            importance = str(getattr(ev, "importance", "")).lower()
            ev_ts      = getattr(ev, "time", None)
            if not ev_ts:
                continue
            if importance not in {"2", "3", "high", "medium-high"}:
                continue
            if currency.upper() not in _IMPACT_CURRENCIES:
                continue
            delta_secs = float(ev_ts.timestamp() if hasattr(ev_ts, "timestamp") else ev_ts) - now_ts
            if 0 <= delta_secs:
                delta_mins = delta_secs / 60.0
                if min_delta is None or delta_mins < min_delta:
                    min_delta = delta_mins

        if min_delta is not None:
            _log.debug("[NewsCalendar] MT5: next high-impact in %.1f min", min_delta)
        return min_delta

    except Exception as e:
        _log.debug("[NewsCalendar] MT5 source error: %s", e)
        return None


# ── Source 2: Finnhub ─────────────────────────────────────────────────────────

def _from_finnhub() -> Optional[float]:
    """Query Finnhub economic calendar. Requires FINNHUB_API_KEY in config."""
    try:
        from backend.src.config import get as cfg_get
        api_key = cfg_get("finnhub_api_key", "")
        if not api_key:
            return None

        import urllib.request, json as _json, datetime as _dt
        now    = time.time()
        today  = _dt.date.fromtimestamp(now).isoformat()
        end_d  = _dt.date.fromtimestamp(now + 7200).isoformat()
        url    = (
            f"https://finnhub.io/api/v1/calendar/economic"
            f"?from={today}&to={end_d}&token={api_key}"
        )
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json.loads(resp.read())

        events = data.get("economicCalendar") or []
        min_delta = None
        for ev in events:
            if str(ev.get("impact", "")).lower() not in _HIGH_IMPACT:
                continue
            if (ev.get("country", "") or "").upper() not in {"US", "EU", "GB", "XAU"}:
                continue
            ev_ts = ev.get("time")
            if not ev_ts:
                continue
            import dateutil.parser as _dp
            ev_epoch = _dp.parse(ev_ts).timestamp()
            delta_mins = (ev_epoch - now) / 60.0
            if 0 <= delta_mins:
                if min_delta is None or delta_mins < min_delta:
                    min_delta = delta_mins

        return min_delta

    except Exception as e:
        _log.debug("[NewsCalendar] Finnhub source error: %s", e)
        return None


# ── Source 3: ForexFactory scrape ─────────────────────────────────────────────

def _from_forexfactory() -> Optional[float]:
    """
    Minimal ForexFactory calendar scrape.
    Only runs when both MT5 and Finnhub are unavailable.
    Uses the JSON feed that FF exposes for their mobile apps.
    """
    try:
        import urllib.request, json as _json, datetime as _dt
        now    = time.time()
        url    = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        req    = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            events = _json.loads(resp.read())

        min_delta = None
        for ev in events:
            if (ev.get("impact", "") or "").lower() not in {"high", "red"}:
                continue
            if (ev.get("currency", "") or "").upper() not in _IMPACT_CURRENCIES:
                continue
            date_str = ev.get("date")
            if not date_str:
                continue
            try:
                import dateutil.parser as _dp
                ev_epoch = _dp.parse(date_str).timestamp()
            except Exception:
                continue
            delta_mins = (ev_epoch - now) / 60.0
            if 0 <= delta_mins <= 120:
                if min_delta is None or delta_mins < min_delta:
                    min_delta = delta_mins

        return min_delta

    except Exception as e:
        _log.debug("[NewsCalendar] ForexFactory source error: %s", e)
        return None


# ── Forced-refresh helper (call after a known news event passes) ──────────────

def invalidate_cache() -> None:
    """Ask the background refresher to re-fetch on its next wake (call after a
    known event passes). Also starts the refresher if it isn't running."""
    ensure_started()
    _wake.set()
