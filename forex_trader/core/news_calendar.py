"""
Economic news calendar — the single source of truth for calendar events.

Source: the ForexFactory weekly JSON feed published by faireconomy
(https://nfs.faireconomy.media/ff_calendar_thisweek.json). No API key, no rate
limit, impact already rated per event. Only the current week is published —
`ff_calendar_nextweek.json` and friends return 404 — so the horizon is however
much of the current Mon–Sun window is left.

Feed schema (verified against the live feed):
    {"title": str, "country": str, "date": ISO-8601 with offset,
     "impact": "High"|"Medium"|"Low"|"Holiday", "forecast": str, "previous": str}

Note `country`, not `currency` — it carries currency codes ("USD", "EUR") but
the key is named `country`. Reading it as `currency` yields None for every
event, which silently disables anything built on top; that was the state of
this module and of test_signal/news_filter.py before this was fixed.

Two consumers:
  * ML engines, via get_news_proximity_norm() — minutes to the next relevant
    high-impact event, normalised to [0,1]. 0=imminent, 1=safe/far.
  * The signal generators' news blackout, via is_high_impact_window(), and the
    News tab, via get_events().

Fetch failures are never fatal: get_events() serves the last good payload, and
the blackout falls back to a hardcoded schedule of the routine gold movers.
"""
from __future__ import annotations

import logging
import ssl
import time
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger(__name__)

_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# macOS Python bundles its own SSL without system roots — build a context that
# trusts certifi's bundle so the HTTPS call succeeds off a stock python.org build.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# ── Fetch cache ───────────────────────────────────────────────────────────────
# _next_fetch_ts is tracked explicitly rather than derived from the last success,
# so a failure can schedule its own retry. The feed answers 429 under repeated
# calls, and without a backoff a cold cache would re-request on every single
# get_events() — several engines poll this per cycle.
_cache_events:  Optional[list[dict]] = None
_next_fetch_ts: float = 0.0
_CACHE_TTL:     float = 1800.0   # 30 min; the feed is a weekly publish
_RETRY_AFTER:   float = 300.0    # 5 min backoff after a failed fetch

# ── Gold relevance ────────────────────────────────────────────────────────────
# XAUUSD is priced in dollars, so USD events dominate. The majors move gold via
# the dollar index and via risk sentiment, and get a fractional weight; the rest
# are kept in the feed for display but score near zero.
_CURRENCY_WEIGHT: dict[str, float] = {
    "USD": 1.0, "XAU": 1.0, "ALL": 0.6,
    "EUR": 0.5, "GBP": 0.4, "CNY": 0.4,
    "JPY": 0.3, "CHF": 0.25,
    "CAD": 0.15, "AUD": 0.15, "NZD": 0.1,
}
_DEFAULT_CURRENCY_WEIGHT = 0.1

# Events that historically move gold harder than their impact rating implies.
_KEYWORD_BOOST: tuple[tuple[str, float], ...] = (
    ("fomc", 1.0), ("federal funds", 1.0), ("rate decision", 0.9),
    ("powell", 0.8), ("fed chair", 0.8), ("press conference", 0.6),
    ("non-farm", 0.9), ("nonfarm", 0.9), ("unemployment rate", 0.6),
    ("cpi", 0.8), ("core pce", 0.8), ("ppi", 0.5),
    ("gdp", 0.5), ("ism", 0.5), ("retail sales", 0.5),
    ("unemployment claims", 0.4), ("jolts", 0.3),
)

_IMPACT_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1, "holiday": 0}

# Impact sets the blackout may be configured against.
_IMPACT_SETS: dict[str, frozenset[str]] = {
    "high":        frozenset({"high"}),
    "high_medium": frozenset({"high", "medium"}),
}

# Blackout defaults — used when the key is absent from config.yaml.
_DEF_BLACKOUT_ENABLED = True
_DEF_BLACKOUT_IMPACT  = "high"
_DEF_MINUTES_BEFORE   = 30
_DEF_MINUTES_AFTER    = 30

# FOMC 2026 announcement dates (UTC), rate decision ~19:00, presser ~19:30.
# Last-resort fallback used only while the feed is unreachable.
_FOMC_DATES_2026: set[tuple[int, int]] = {
    (1, 29), (3, 19), (5, 7), (6, 18),
    (7, 29), (9, 17), (11, 5), (12, 16),
}


# ── Fetch + normalise ─────────────────────────────────────────────────────────

def _fetch_raw() -> list[dict]:
    """Fetch the weekly feed, cached. Returns [] only if we have never succeeded."""
    global _cache_events, _next_fetch_ts
    now = time.time()
    if now < _next_fetch_ts:
        return _cache_events or []

    try:
        import json
        import urllib.request
        req = urllib.request.Request(_FEED_URL, headers={"User-Agent": "ForexTrader/0.5"})
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode())
        if not isinstance(data, list):
            raise ValueError(f"feed returned {type(data).__name__}, expected list")
        _cache_events  = data
        _next_fetch_ts = now + _CACHE_TTL
        _log.info("[NewsCalendar] Loaded %d events from ForexFactory", len(data))
    except Exception as e:
        # Keep serving the last good payload rather than going blind mid-week.
        _next_fetch_ts = now + _RETRY_AFTER
        _log.warning("[NewsCalendar] Feed fetch failed (%s) — serving cached payload", e)
    return _cache_events or []


def _currency_of(raw: dict) -> str:
    """The feed names this field `country`, but it carries a currency code."""
    return str(raw.get("country") or "").strip().upper()


def _gold_score(currency: str, impact: str, title: str) -> float:
    """Relative importance to XAUUSD. Higher is more likely to move gold."""
    rank = _IMPACT_RANK.get(impact, 0)
    if rank == 0:
        return 0.0
    weight = _CURRENCY_WEIGHT.get(currency, _DEFAULT_CURRENCY_WEIGHT)
    lowered = title.lower()
    boost = max((b for kw, b in _KEYWORD_BOOST if kw in lowered), default=0.0)
    return round(rank * weight + boost * weight, 4)


def _normalise(raw: dict) -> Optional[dict]:
    """Feed row -> internal event dict, or None if unparseable."""
    date_str = raw.get("date")
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    title    = str(raw.get("title") or "Untitled event")
    currency = _currency_of(raw)
    impact   = str(raw.get("impact") or "").strip().lower()
    return {
        "title":    title,
        "currency": currency,
        "impact":   impact,
        "ts":       dt.timestamp(),
        "dt":       dt.astimezone(timezone.utc),
        "forecast": str(raw.get("forecast") or ""),
        "previous": str(raw.get("previous") or ""),
        "score":    _gold_score(currency, impact, title),
    }


def get_events(
    impacts: Optional[set[str]] = None,
    currencies: Optional[set[str]] = None,
    upcoming_only: bool = False,
) -> list[dict]:
    """
    All events in the current week, normalised and sorted by time.

    impacts     — lowercase impact names to keep ("high", "medium", ...). None = all.
    currencies  — currency codes to keep. None = all.
    upcoming_only — drop events whose scheduled time has already passed.
    """
    now_ts = time.time()
    out: list[dict] = []
    for raw in _fetch_raw():
        ev = _normalise(raw)
        if ev is None:
            continue
        if impacts is not None and ev["impact"] not in impacts:
            continue
        if currencies is not None and ev["currency"] not in currencies:
            continue
        if upcoming_only and ev["ts"] < now_ts:
            continue
        out.append(ev)
    out.sort(key=lambda e: e["ts"])
    return out


# ── Blackout configuration ────────────────────────────────────────────────────

def get_blackout_settings() -> dict:
    """Blackout settings from config.yaml, with defaults applied and clamped."""
    try:
        from forex_trader import config as cfg
        enabled = bool(cfg.get("news_blackout_enabled", _DEF_BLACKOUT_ENABLED))
        impact  = str(cfg.get("news_blackout_impact", _DEF_BLACKOUT_IMPACT)).lower()
        before  = int(cfg.get("news_blackout_minutes_before", _DEF_MINUTES_BEFORE))
        after   = int(cfg.get("news_blackout_minutes_after", _DEF_MINUTES_AFTER))
    except Exception:
        enabled, impact = _DEF_BLACKOUT_ENABLED, _DEF_BLACKOUT_IMPACT
        before, after = _DEF_MINUTES_BEFORE, _DEF_MINUTES_AFTER

    if impact not in _IMPACT_SETS:
        impact = _DEF_BLACKOUT_IMPACT
    return {
        "enabled": enabled,
        "impact":  impact,
        "impacts": _IMPACT_SETS[impact],
        # 0 is a legitimate "no pre/post padding"; the cap stops a typo from
        # blacking out the whole week.
        "minutes_before": max(0, min(240, before)),
        "minutes_after":  max(0, min(240, after)),
    }


# ── Current-event query (drives the blackout and the top-bar badge) ────────────

def get_current_event(
    minutes_before: Optional[int] = None,
    minutes_after: Optional[int] = None,
    impacts: Optional[set[str]] = None,
) -> Optional[dict]:
    """
    The blackout-relevant event we are currently inside, or None.

    Adds to the event dict: window_start, window_end, mins_to_event (negative
    once the event has passed), mins_remaining. When several windows overlap,
    returns the one that ends last — that is the one the caller must wait out.

    Arguments default to the configured blackout settings.
    """
    settings = get_blackout_settings()
    if minutes_before is None:
        minutes_before = settings["minutes_before"]
    if minutes_after is None:
        minutes_after = settings["minutes_after"]
    if impacts is None:
        impacts = settings["impacts"]

    now_ts = time.time()
    # Gold-relevant currencies only: a high-impact NZD print is not a reason to
    # stop trading XAUUSD.
    events = get_events(impacts=impacts, currencies={"USD", "XAU"})

    best: Optional[dict] = None
    for ev in events:
        window_start = ev["ts"] - minutes_before * 60
        window_end   = ev["ts"] + minutes_after * 60
        if not (window_start <= now_ts <= window_end):
            continue
        candidate = dict(ev)
        candidate.update({
            "event_ts":       ev["ts"],
            "window_start":   window_start,
            "window_end":     window_end,
            "mins_to_event":  round((ev["ts"] - now_ts) / 60, 1),
            "mins_remaining": round((window_end - now_ts) / 60, 1),
        })
        if best is None or candidate["window_end"] > best["window_end"]:
            best = candidate
    return best


def is_high_impact_window(
    minutes_before: Optional[int] = None,
    minutes_after: Optional[int] = None,
) -> bool:
    """
    True when new entries should be suppressed for news.

    Returns False immediately when the blackout is switched off. While the feed
    is unreachable and nothing has ever been cached, falls back to a hardcoded
    schedule of the routine gold movers.
    """
    settings = get_blackout_settings()
    if not settings["enabled"]:
        return False

    if _fetch_raw():
        return get_current_event(minutes_before, minutes_after) is not None
    return _hardcoded_fallback(datetime.now(timezone.utc))


def check_news_blackout() -> tuple[bool, str]:
    """
    Return (allowed, reason) — the same contract as
    core_trading_schedule.check_trading_schedule, so the entry-path call sites
    that already gate on the schedule can gate on news in the same two lines.

    `reason` is empty when allowed, and human-readable when not (it reaches the
    user through skip_reason strings and Telegram alerts).
    """
    if not get_blackout_settings()["enabled"]:
        return True, ""

    ev = get_current_event()
    if ev is None:
        # Feed down and never cached: fall back to the hardcoded schedule.
        if not _fetch_raw() and _hardcoded_fallback(datetime.now(timezone.utc)):
            return False, "News blackout — scheduled high-impact window (calendar unavailable)"
        return True, ""

    return False, (
        f"News blackout — {ev['title']} ({ev['currency']}), "
        f"resumes in {int(round(ev['mins_remaining']))} min"
    )


def _hardcoded_fallback(now: datetime) -> bool:
    """Feed-free approximation: FOMC days, NFP Friday, CPI Tuesday, top of hour."""
    m, h, dow = now.minute, now.hour, now.weekday()
    if (now.month, now.day) in _FOMC_DATES_2026 and 12 <= h < 22:
        return True
    if dow == 4 and now.day <= 7 and 12 <= h < 16:
        return True
    if dow == 1 and 8 <= now.day <= 22 and ((h == 13 and m >= 15) or (h == 14 and m <= 30)):
        return True
    if h in (7, 8, 13, 14, 15, 16) and m < 5:
        return True
    return False


# ── ML feature ────────────────────────────────────────────────────────────────

def _mins_to_norm(minutes: Optional[float], window: float = 120.0) -> float:
    """Minutes-to-event -> [0,1]. 0=imminent, 1=far/safe."""
    if minutes is None:
        return 1.0
    return round(min(1.0, max(0.0, float(minutes) / window)), 4)


def get_news_proximity_norm(window_minutes: float = 120.0) -> float:
    """
    news_proximity_norm in [0,1] — minutes to the next high-impact USD/gold
    event over `window_minutes`, clamped. Returns 1.0 (safe) when the calendar
    is unavailable: better to trade on an unclear calendar than to have a broken
    feed quietly poison a model input.
    """
    try:
        now_ts = time.time()
        upcoming = get_events(
            impacts={"high"},
            currencies={"USD", "XAU"},
            upcoming_only=True,
        )
        if not upcoming:
            return 1.0
        return _mins_to_norm((upcoming[0]["ts"] - now_ts) / 60.0, window_minutes)
    except Exception as e:
        _log.debug("[NewsCalendar] proximity calc failed: %s", e)
        return 1.0


def invalidate_cache() -> None:
    """Force the next call to re-fetch the feed, bypassing any active backoff."""
    global _next_fetch_ts
    _next_fetch_ts = 0.0
