"""
External market context: DXY (dollar index), US 10-Year yield, VIX, GVZ (gold
volatility), and US real yield proxy (TIP ETF).
Uses yfinance with a 15-minute in-process cache.
Falls back to neutral values when yfinance is unavailable or market is closed.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

_log = logging.getLogger("test_signal")

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

# {cache_key: (timestamp, closes)}
_CACHE: dict[str, tuple[float, list[float]]] = {}
_CACHE_TTL = 15 * 60  # 15 minutes
# Closes fetched and cached per symbol. Every caller asks for <= this, so one
# fetch serves all of them -- see _get_hourly_closes.
_FETCH_WINDOW = 5

_NEUTRAL = {
    "dxy_momentum":  0.0,
    "us10y_level":   4.5,
    "vix_level":    20.0,
    "gvz_level":    17.0,   # CBOE Gold Volatility Index typical mid-range
    "tip_momentum":  0.0,   # TIP ETF 1h return; negative → real yields rising → gold headwind
}


def _get_hourly_closes(symbol: str, n: int = 5) -> list[float]:
    """Last `n` hourly closes for `symbol`, served from a 15-minute cache.

    The cache stores the whole `_FETCH_WINDOW`-long list, not just its last
    value. That matters twice: the hit branch can actually return something,
    and one fetch serves every caller whatever `n` they ask for --
    `_dxy_momentum` wants 3 and `_latest` wants 2 off overlapping symbols.

    The original shape packed a single float in here, so the hit branch could
    not reconstruct the list and fell through to re-fetch on every call. The
    documented 15-minute cache had therefore never once prevented a request:
    every `get_context()` was five live yfinance round trips. Found while
    speccing this module onto the Reversal Engine's 60s cycle
    (docs/todo/001-reversal-macro-context.md), where that would have put five
    blocking HTTP calls inside the async engine loop every minute.
    """
    now = time.time()
    key = f"{symbol}_closes"
    hit = _CACHE.get(key)
    if hit is not None:
        ts, cached = hit
        if (now - ts) < _CACHE_TTL:
            return cached[-n:]
    if not _YF_AVAILABLE:
        return []
    try:
        hist = yf.Ticker(symbol).history(period="5d", interval="1h", auto_adjust=True)
        if hist.empty:
            return []
        closes = [float(v) for v in hist["Close"].dropna().tolist()[-_FETCH_WINDOW:]]
        _CACHE[key] = (now, closes)
        return closes[-n:]
    except Exception as e:
        _log.debug("[MarketCtx] %s fetch error: %s", symbol, e)
        return []


def _dxy_momentum() -> float:
    """DXY 1-hour return normalised to [-1, +1]. 0.5% change = ±1.0."""
    closes = _get_hourly_closes("DX-Y.NYB", n=3)
    if len(closes) < 2:
        return 0.0
    pct = (closes[-1] - closes[-2]) / closes[-2] * 100
    return round(max(-1.0, min(1.0, pct / 0.5)), 4)


def _tip_momentum() -> float:
    """TIP ETF (inflation-protected bonds) 1-hour return normalised to [-1, +1].
    Rising TIP = falling real yields = gold tailwind (+1).
    Falling TIP = rising real yields = gold headwind (-1).
    0.2% change = ±1.0.
    """
    closes = _get_hourly_closes("TIP", n=3)
    if len(closes) < 2:
        return 0.0
    pct = (closes[-1] - closes[-2]) / closes[-2] * 100
    return round(max(-1.0, min(1.0, pct / 0.2)), 4)


def _latest(symbol: str) -> Optional[float]:
    closes = _get_hourly_closes(symbol, n=2)
    return closes[-1] if closes else None


def get_context() -> dict:
    """
    Returns macro features safe for ML models (all finite floats):
      dxy_momentum  (-1..+1)  — DXY hourly momentum; negative = USD weakening = gold tailwind
      us10y_level   (%)       — US 10-year nominal yield
      vix_level               — CBOE VIX equity fear gauge
      gvz_level               — CBOE Gold Volatility Index; >20 = elevated gold vol
      tip_momentum  (-1..+1)  — TIP ETF hourly return proxy for real yield direction
    """
    dxy   = _dxy_momentum()
    t10y  = _latest("^TNX") or _NEUTRAL["us10y_level"]
    vix   = _latest("^VIX") or _NEUTRAL["vix_level"]
    gvz   = _latest("^GVZ") or _NEUTRAL["gvz_level"]
    tip   = _tip_momentum()
    ctx = {
        "dxy_momentum":  dxy,
        "us10y_level":   round(float(t10y), 4),
        "vix_level":     round(float(vix),  4),
        "gvz_level":     round(float(gvz),  4),
        "tip_momentum":  tip,
    }
    _log.debug(
        "[MarketCtx] DXY=%+.3f US10Y=%.2f%% VIX=%.1f GVZ=%.1f TIP=%+.3f",
        dxy, ctx["us10y_level"], ctx["vix_level"], ctx["gvz_level"], tip,
    )
    return ctx
