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
import time
from typing import Optional

_log = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache_ts:           float = 0.0
_cache_next_mins:    Optional[float] = None   # minutes to next high-impact event
_CACHE_TTL:          float = 600.0            # 10 minutes

# Currencies that affect XAUUSD meaningfully
_IMPACT_CURRENCIES = {"USD", "XAU", "US", "EUR", "GBP"}
_HIGH_IMPACT       = {"high", "3", "red"}     # Finnhub uses "3"; FF uses "red"/"high"


def _mins_to_norm(minutes: Optional[float], window: float = 120.0) -> float:
    """Convert minutes-to-event to a [0,1] norm. 0=imminent, 1=far/safe."""
    if minutes is None:
        return 1.0
    return round(min(1.0, max(0.0, float(minutes) / window)), 4)


def get_news_proximity_norm(window_minutes: float = 120.0) -> float:
    """
    Return news_proximity_norm [0,1].
    Cached for 10 minutes. Always returns 1.0 (safe) on any error — better to
    trade on unclear calendar than to block all signals from a broken feed.
    """
    global _cache_ts, _cache_next_mins
    now = time.time()
    if _cache_next_mins is not None and (now - _cache_ts) < _CACHE_TTL:
        return _mins_to_norm(_cache_next_mins, window_minutes)

    mins = _fetch_next_event_minutes()
    _cache_next_mins = mins
    _cache_ts = now
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
        from forex_trader.core import mt5_bridge as bridge
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
    global _cache_ts
    _cache_ts = 0.0
