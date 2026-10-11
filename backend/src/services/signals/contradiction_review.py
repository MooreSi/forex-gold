"""Contradiction review: two sources disagree, the market decides which runs.

docs/simon-handover/041, owner 2026-10-11: "add into risk settings a
contradiction review that uses its ml/ai capabilities to evaluate the market
to determine which one should be executed". Plan and provisional choices:
docs/todo/signals/020-contradiction-review.md.

WHAT IT DOES
------------
Called beside the trading-schedule gate on every automated order route. If a
DIFFERENT source has an opposing signal live on the signal bus within
WINDOW_S (any pairing: channel/channel, channel/engine, engine/engine), it:

1. reads the market: H1 and H4 trend bias, M15 ADX (regime) and MACD
   momentum, from the bridge;
2. asks the configured AI provider which side to execute, BUY, SELL or
   neither, given both signals and that read;
3. if the AI is not configured, fails, or answers something unreadable, the
   indicator vote decides (H1 bias, H4 bias, MACD momentum: one vote each).

The signal in hand is refused when the answer is the other side, or
"neither". Otherwise it goes ahead.

WHAT IT CAN NEVER DO
--------------------
Close, cancel or modify what is already live. It answers allow/refuse for the
signal in hand, nothing more. Cancelling an opposing PENDING signal would need
fill state on the bus (stage 2 item 1 of docs/todo/signals/010), and closing a
filled one is the frozen close path. A test scans this file for those calls.

FAILS OPEN
----------
Off, no opposing signal, no AI and no market read, or any exception: allow,
which is exactly what the app did before this existed. A restriction nobody
could justify is the expensive direction of wrong (same rule as
contradiction.py).

It never raises, and a verdict is cached for CACHE_S so a signal that passes
two gates (scan_auto_execute, then the shared open path) costs one AI call.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.ai import provider as _provider
from backend.src.services.market.indicators import (
    H4_BIAS_MIN_CANDLES, compute_adx, compute_h4_bias, compute_macd_hist, detect_regime,
)
from backend.src.services.market.levels import compute_htf_bias
from backend.src.services.signals import contradiction as _c

log = logging.getLogger(__name__)

SETTING_KEY = "contradiction_review_enabled"

# The shipped study policies' window: an opposing signal older than this is
# not "at the same time".
WINDOW_S = 600.0
CACHE_S = 120.0
AI_TIMEOUT_S = 12

_LENS = _c.Policy("review", _c.MODE_FIRST_WINS, window_secs=WINDOW_S, pairs=_c.ALL_PAIRS)
_cache: dict[tuple, tuple[float, tuple[bool, str]]] = {}

_SYSTEM = (
    "You are a risk reviewer for an automated XAUUSD (gold) trading system. "
    "Two signal sources disagree on direction at the same time. Using the "
    "market read provided, decide which direction should be executed now, or "
    "neither if the market favours neither. Respond ONLY with one minified "
    'JSON object: {"execute":"BUY"|"SELL"|"neither","confidence":0.0-1.0,'
    '"reason":"one sentence"}'
)


# The order paths name an engine by its display name; the bus names it by
# the string the engine writes (reversal_engine_service, breakout_signal_
# service). Without this an engine that flipped within the window would be
# reviewed against itself. Trend PA does not write the bus.
_ENGINE_BUS_NAMES = {
    "reversal engine": "reversal_engine",
    "breakout engine": "breakout",
    "trend pa engine": "trend_pa",
}


def source_of(source_name: str) -> tuple[str, str]:
    """(source_kind, bus name) for a source as an order path names it."""
    key = (source_name or "").strip().lower()
    if key in _ENGINE_BUS_NAMES:
        return _c.KIND_ENGINE, _ENGINE_BUS_NAMES[key]
    if key in _ENGINE_BUS_NAMES.values():
        return _c.KIND_ENGINE, key
    return _c.KIND_TELEGRAM, source_name or ""


async def review_source(rs: dict, bridge: Any, source_name: str, direction: str,
                        symbol: str = "XAUUSD") -> tuple[bool, str]:
    """`review` for a caller that has only the order path's source name."""
    kind, name = source_of(source_name)
    return await review(rs, bridge, source_kind=kind, source_name=name,
                        direction=direction, symbol=symbol)


def enabled(rs: dict) -> bool:
    try:
        return bool(int(rs.get(SETTING_KEY, 0) or 0))
    except Exception:
        return False


def _load_cfg() -> dict:
    from backend.src import config as _config
    return _config.load()


def _opposing(source_kind: str, source_name: str, direction: str, symbol: str,
              now: float) -> list[_c.Active]:
    rows = db_module.get_concurrent_signals(
        exclude_engine=source_name, window_seconds=WINDOW_S,
        kinds=(_c.KIND_ENGINE, _c.KIND_TELEGRAM), symbol=symbol or None,
    )
    active = [
        _c.Active(
            source_kind=str(r.get("source_kind") or _c.KIND_ENGINE),
            source_name=str(r.get("source_name") or r.get("engine") or ""),
            direction=str(r.get("direction") or ""),
            symbol=str(r.get("symbol") or ""),
            created_at=float(r.get("created_at") or 0.0),
            is_pending=None,
            signal_ref=str(r.get("signal_id") or ""),
        )
        for r in rows
    ]
    cand = _c.Candidate(source_kind=source_kind, source_name=source_name,
                        direction=direction, symbol=symbol, at=now)
    return _c.opposing_entries(cand, active, _LENS, now)


async def evaluate_market(bridge: Any) -> dict:
    """H1/H4 bias, M15 ADX, regime and MACD momentum. {} if unreadable."""
    try:
        h1 = await bridge.get_candles("H1", 80) or []
        h4 = await bridge.get_candles("H4", H4_BIAS_MIN_CANDLES + 8) or []
        m15 = await bridge.get_candles("M15", 80) or []
    except Exception as exc:
        log.debug("[ContradictionReview] market read failed: %s", exc)
        return {}
    if not (h1 or h4 or m15):
        return {}
    h1_bias, h4_bias = compute_htf_bias(h1), compute_h4_bias(h4)
    closes = [float(c["close"]) for c in m15 if c.get("close")]
    adx = compute_adx(m15) if len(m15) > 30 else 0.0
    _macd, hist = compute_macd_hist(closes) if len(closes) > 35 else (0.0, 0.0)
    return {
        "h1_bias": h1_bias, "h4_bias": h4_bias, "adx_m15": round(float(adx), 1),
        "macd_hist_m15": float(hist), "regime": detect_regime(adx, h1_bias, h4_bias),
        "last_price": closes[-1] if closes else None,
    }


def indicator_vote(market: dict) -> Optional[str]:
    """BUY, SELL or None (split or no read). One vote each: H1 bias, H4 bias,
    MACD momentum."""
    score = 0
    for key in ("h1_bias", "h4_bias"):
        score += {"bullish": 1, "bearish": -1}.get(market.get(key, ""), 0)
    hist = float(market.get("macd_hist_m15") or 0.0)
    if abs(hist) > 1e-6:
        score += 1 if hist > 0 else -1
    if score > 0:
        return "BUY"
    if score < 0:
        return "SELL"
    return None


async def _ask_ai(source_name: str, direction: str, opposing: list[_c.Active],
                  market: dict) -> Optional[str]:
    """BUY, SELL, NEITHER, or None when the AI gave no usable answer."""
    try:
        cfg = _load_cfg()
        if not _provider.is_configured(cfg):
            return None
        others = "; ".join(f"{a.source_name} ({a.source_kind}) says {a.direction.upper()}, "
                           f"{max(0, int(time.time() - a.created_at))}s ago" for a in opposing)
        prompt = (f"New signal: {source_name} says {direction.upper()}.\n"
                  f"Opposing live signal(s): {others}.\n"
                  f"Market read: {json.dumps(market, sort_keys=True)}")
        raw = await _provider.complete(cfg, _SYSTEM, prompt, max_tokens=200,
                                       timeout=AI_TIMEOUT_S)
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not m:
            return None
        choice = str(json.loads(m.group(0)).get("execute", "")).strip().upper()
        return choice if choice in ("BUY", "SELL", "NEITHER") else None
    except Exception as exc:
        log.info("[ContradictionReview] AI unavailable (%s); using the indicators", exc)
        return None


async def review(rs: dict, bridge: Any, *, source_kind: str, source_name: str,
                 direction: str, symbol: str = "XAUUSD",
                 now: Optional[float] = None) -> tuple[bool, str]:
    """(True, "") to go ahead, (False, reason) to refuse. Never raises."""
    if not enabled(rs):
        return True, ""
    try:
        now = float(now if now is not None else time.time())
        direction = (direction or "").strip().upper()
        opposing = await db_module.to_db_thread(
            _opposing, source_kind, source_name, direction, symbol, now)
        if not opposing:
            return True, ""
        key = (source_name.strip().lower(), direction,
               tuple(sorted((a.source_name, a.created_at) for a in opposing)))
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]

        market = await evaluate_market(bridge)
        choice, how = await _ask_ai(source_name, direction, opposing, market), "AI"
        if choice is None:
            choice, how = indicator_vote(market), "indicators"
        who = ", ".join(f"{a.source_name} {a.direction.upper()}" for a in opposing)
        if choice is None or choice == direction:
            result = (True, "")
        else:
            side = "neither side" if choice == "NEITHER" else choice
            result = (False, f"contradiction review: opposing {who}; {how} favour "
                             f"{side}, so this {direction} is not taken")
        log.info("[ContradictionReview] %s %s vs %s -> %s (%s, market=%s)",
                 source_name, direction, who, choice or "undecided", how, market)
        _cache[key] = (now, result)
        return result
    except Exception as exc:
        log.warning("[ContradictionReview] failed open for %s %s: %s",
                    source_name, direction, exc)
        return True, ""
