"""
Live economic calendar filter.
Fetches Forex Factory's public JSON feed; falls back to hardcoded schedule.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

# macOS Python bundles its own SSL without system roots — create a context that
# trusts the OS certificate store so the Forex Factory HTTPS call succeeds.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

from backend.src.config import is_debug as _is_debug

_log = logging.getLogger("test_signal")

_CACHE: Optional[list] = None
_CACHE_TS: float = 0.0
_CACHE_TTL = 4 * 3600  # refresh every 4 hours


# FOMC 2026 dates — fallback only
_FOMC_DATES_2026: set[tuple[int, int]] = {
    (1, 29), (3, 19), (5, 7), (6, 18),
    (7, 29), (9, 17), (11, 5), (12, 16),
}


def _fetch_calendar() -> list:
    global _CACHE, _CACHE_TS
    now = time.time()
    if _CACHE is not None and (now - _CACHE_TS) < _CACHE_TTL:
        return _CACHE
    if _is_debug():
        # Canned calendar: one high-impact USD event ~2h out, so the
        # proximity/window code runs against real-shaped data with zero
        # outbound requests.
        ev_dt = datetime.fromtimestamp(now + 7200, tz=timezone.utc)
        _CACHE = [{
            "title": "[debug] FOMC Statement (canned)", "currency": "USD",
            "impact": "High", "date": ev_dt.isoformat().replace("+00:00", "Z"),
        }]
        _CACHE_TS = now
        return _CACHE
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        req = urllib.request.Request(url, headers={"User-Agent": "ForexTrader/0.5"})
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode())
        _CACHE = data if isinstance(data, list) else []
        _CACHE_TS = now
        _log.info("[NewsFilter] Loaded %d events from Forex Factory", len(_CACHE))
        return _CACHE
    except Exception as e:
        _log.debug("[NewsFilter] Calendar fetch failed (%s) — using hardcoded fallback", e)
        return _CACHE or []


def get_current_event(
    minutes_before: int = 30,
    minutes_after: int = 30,
) -> Optional[dict]:
    """
    Return details of the active high-impact event we are currently inside, or None.
    Dict keys: title, currency, event_ts (unix), window_start, window_end,
               mins_to_event (negative = event already passed), mins_remaining.
    """
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    events = _fetch_calendar()
    if not events:
        return None

    best = None
    for ev in events:
        if ev.get("currency") not in ("USD", "XAU"):
            continue
        if ev.get("impact") != "High":
            continue
        ev_date_str = ev.get("date", "")
        if not ev_date_str:
            continue
        try:
            ev_dt = datetime.fromisoformat(ev_date_str.replace("Z", "+00:00"))
            ev_ts = ev_dt.timestamp()
        except Exception:
            continue
        window_start = ev_ts - minutes_before * 60
        window_end   = ev_ts + minutes_after * 60
        if window_start <= now_ts <= window_end:
            candidate = {
                "title":        ev.get("title", "High-Impact Event"),
                "currency":     ev.get("currency", "USD"),
                "event_ts":     ev_ts,
                "window_start": window_start,
                "window_end":   window_end,
                "mins_to_event":   round((ev_ts - now_ts) / 60, 1),
                "mins_remaining":  round((window_end - now_ts) / 60, 1),
            }
            # Pick the event whose window_end is furthest in the future (longest remaining)
            if best is None or candidate["window_end"] > best["window_end"]:
                best = candidate
    return best


def is_high_impact_window(minutes_before: int = 30, minutes_after: int = 30) -> bool:
    """
    True if within minutes_before/after of a high-impact USD or Gold event.
    Uses live Forex Factory feed; falls back to hardcoded dates on failure.
    """
    now = datetime.now(timezone.utc)
    events = _fetch_calendar()

    if events:
        if get_current_event(minutes_before, minutes_after) is not None:
            return True
        return False

    return _hardcoded_fallback(now)


def _hardcoded_fallback(now: datetime) -> bool:
    m, h, dow = now.minute, now.hour, now.weekday()
    if (now.month, now.day) in _FOMC_DATES_2026 and 12 <= h < 22:
        return True
    if dow == 4 and now.day <= 7 and 12 <= h < 16:
        return True
    if dow == 1 and 8 <= now.day <= 22 and (
        (h == 13 and m >= 15) or (h == 14 and m <= 30)
    ):
        return True
    if h in (7, 8, 13, 14, 15, 16) and m < 5:
        return True
    return False
