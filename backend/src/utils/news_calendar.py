"""
Economic news calendar — the single source of truth for calendar events.

Source: the ForexFactory weekly JSON feed published by faireconomy
(https://nfs.faireconomy.media/ff_calendar_thisweek.json). No API key, and
impact is already rated per event. It DOES rate-limit: repeated calls earn a
429 (confirmed live 2026-08-07), which is why _fetch_raw() caches to disk as
well as in memory, backs off progressively, honours Retry-After, and asks
conditionally. Only the current week is published —
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
import threading
import ssl
import time
from datetime import datetime, timezone
from typing import Optional

from backend.src.config import is_debug as _is_debug

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
_RETRY_AFTER:   float = 300.0    # 5 min backoff after the first failed fetch
_RETRY_MAX:     float = 3600.0   # ceiling for the doubling below

# Consecutive failures, for backoff. A flat 5-minute retry is what turns a
# transient 429 into a sustained one: the feed says "slow down" and the client
# keeps asking at the same rate until it relents. Doubling from _RETRY_AFTER
# means the first failure still retries in 5 min (unchanged) and a feed that
# stays angry is left alone.
_fail_streak: int = 0

# ETag / Last-Modified from the last successful fetch, replayed as a
# conditional request. A 304 costs the server almost nothing and keeps us
# inside whatever budget the 429 was defending.
_validators: dict[str, str] = {}

# The payload also lives on disk. In memory alone it dies with the process,
# and this app restarts often -- licence activation, self-update, the
# self-healer -- so every restart was a fresh request against a feed that
# publishes weekly. Disk cache collapses a restart storm to one fetch.
_disk_loaded: bool = False

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

def _disk_cache_file():
    """Where the last good payload is kept between runs, or None if unknown."""
    try:
        from pathlib import Path
        from backend.src.config import USER_DATA_DIR
        return Path(USER_DATA_DIR) / "data" / "news_calendar_cache.json"
    except Exception:
        return None


def _load_disk_cache() -> None:
    """Seed the in-memory cache from disk, once per process."""
    global _cache_events, _next_fetch_ts, _validators, _disk_loaded
    _disk_loaded = True
    path = _disk_cache_file()
    if path is None:
        return
    try:
        if not path.exists():
            return
        import json
        blob = json.loads(path.read_text(encoding="utf-8"))
        events = blob.get("events")
        if not isinstance(events, list) or not events:
            return
        _cache_events = events
        vals = blob.get("validators")
        if isinstance(vals, dict):
            _validators = {k: v for k, v in vals.items() if isinstance(v, str)}
        # Whatever TTL is left from when it was written. Already expired is
        # fine and normal: the events are served immediately while the refresh
        # happens, which is the whole point of keeping them.
        _next_fetch_ts = float(blob.get("fetched_at") or 0.0) + _CACHE_TTL
        _log.info("[NewsCalendar] Loaded %d events from the on-disk cache", len(events))
    except Exception as e:
        _log.debug("[NewsCalendar] Could not read the on-disk cache: %s", e)


def _save_disk_cache(events: list[dict]) -> None:
    path = _disk_cache_file()
    if path is None:
        return
    try:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fetched_at": time.time(),
            "validators": _validators,
            "events":     events,
        }), encoding="utf-8")
    except Exception as e:
        _log.debug("[NewsCalendar] Could not write the on-disk cache: %s", e)


def _retry_delay() -> float:
    """Backoff for the current failure streak — 5 min, 10, 20, capped at 1 h."""
    if _fail_streak <= 1:
        return _RETRY_AFTER
    return min(_RETRY_AFTER * (2 ** (_fail_streak - 1)), _RETRY_MAX)


def _retry_after_header(err) -> Optional[float]:
    """Seconds requested by a 429/503 Retry-After header, if it gave one.

    Honouring the server's own number is the difference between backing off
    and guessing. Accepts the delta-seconds form; an HTTP-date is ignored in
    favour of our own backoff rather than parsed, since the feed sends
    seconds. Clamped so a hostile or mistaken value cannot park the calendar
    for a week -- being blind to news is a trading risk, not just a nuisance.
    """
    try:
        value = err.headers.get("Retry-After")
    except Exception:
        return None
    if not value:
        return None
    try:
        return max(_RETRY_AFTER, min(float(str(value).strip()), _RETRY_MAX))
    except (TypeError, ValueError):
        return None


def _fetch_raw() -> list[dict]:
    """Fetch the weekly feed, cached. Returns [] only if we have never succeeded."""
    global _cache_events, _next_fetch_ts, _fail_streak, _validators
    if not _disk_loaded and _cache_events is None:
        _load_disk_cache()

    now = time.time()
    if now < _next_fetch_ts:
        return _cache_events or []

    try:
        import json
        import urllib.error
        import urllib.request
        headers = {"User-Agent": "ForexTrader/0.5"}
        # Only worth asking "has it changed?" when we still hold the answer.
        if _cache_events:
            if _validators.get("etag"):
                headers["If-None-Match"] = _validators["etag"]
            if _validators.get("last_modified"):
                headers["If-Modified-Since"] = _validators["last_modified"]
        req = urllib.request.Request(_FEED_URL, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
                body = resp.read().decode()
                new_validators = {}
                if resp.headers.get("ETag"):
                    new_validators["etag"] = resp.headers["ETag"]
                if resp.headers.get("Last-Modified"):
                    new_validators["last_modified"] = resp.headers["Last-Modified"]
        except urllib.error.HTTPError as http_err:
            if http_err.code == 304:
                # Unchanged since last time — the cheap path, and a success.
                _fail_streak   = 0
                _next_fetch_ts = now + _CACHE_TTL
                _log.debug("[NewsCalendar] Feed unchanged (304) — cache still current")
                return _cache_events or []
            raise

        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError(f"feed returned {type(data).__name__}, expected list")
        _cache_events  = data
        _validators    = new_validators
        _fail_streak   = 0
        _next_fetch_ts = now + _CACHE_TTL
        _save_disk_cache(data)
        _log.info("[NewsCalendar] Loaded %d events from ForexFactory", len(data))
    except Exception as e:
        # Keep serving the last good payload rather than going blind mid-week.
        _fail_streak += 1
        delay = _retry_after_header(e) or _retry_delay()
        _next_fetch_ts = now + delay
        _log.warning(
            "[NewsCalendar] Feed fetch failed (%s) — serving cached payload, "
            "retrying in %.0fs (failure %d)", e, delay, _fail_streak,
        )
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
        from backend.src import config as cfg
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


# Debug mode's canned answer: one high-impact event two hours out, so the
# news-proximity code path is exercised (a norm below 1.0) without being
# inside any blackout window.
_DEBUG_CANNED_MINUTES = 120.0


def _fetch_next_event_minutes() -> Optional[float]:
    """Minutes to the next high-impact USD/gold event, or None.

    Runs on the background refresher thread ONLY -- never on a caller's thread.
    Reads through get_events(), which owns the feed, its 30-minute cache, the
    on-disk cache and the failure backoff.

    Replaces the pre-merge source ladder (_from_mt5 / _from_finnhub /
    _from_forexfactory), deleted upstream on 2026-08-06 (9e8172e): the first
    called a bridge method that does not exist, the second is a premium
    endpoint, and the third read the feed's currency field as `currency` when
    the feed names it `country` -- so every event came back with currency None
    and nothing ever matched the USD/XAU filter. Returns None (treated as
    "unknown", i.e. norm 1.0) when the calendar cannot be read, which keeps the
    long-standing "unclear calendar -> trade through" fallback (Q004).
    """
    if _is_debug():
        return _DEBUG_CANNED_MINUTES
    try:
        upcoming = get_events(
            impacts={"high"}, currencies={"USD", "XAU"}, upcoming_only=True,
        )
    except Exception as e:
        _log.debug("[NewsCalendar] get_events failed: %s", e)
        return None
    if not upcoming:
        return None
    return max(0.0, (upcoming[0]["ts"] - time.time()) / 60.0)


def invalidate_cache() -> None:
    """Ask the background refresher to re-fetch on its next wake (call after a
    known event passes), and clear any active fetch backoff so the next
    get_events() goes to the feed rather than serving the cached payload."""
    global _next_fetch_ts
    _next_fetch_ts = 0.0
    ensure_started()
    _wake.set()
