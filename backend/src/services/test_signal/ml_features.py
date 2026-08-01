"""Feature extraction for the Bounce engine's ML scorer -- split verbatim
from ml_engine.py (M2 file-size pass): FEATURE_NAMES, the indicator helpers,
extract_features and to_vector. ml_engine.py imports these back and
re-exports them, so callers keep addressing ml_engine.<name>; nothing here
imports ml_engine, so the dependency is one-way.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

FEATURE_NAMES = [
    # -- Momentum / oscillator --
    "rsi_m15",          # RSI(14) of last 15 M15 bars
    "rsi_slope",        # RSI change over last 3 bars
    "rsi_divergence",   # +1 bullish div / -1 bearish / 0 none
    # -- Volatility --
    "atr_ratio",        # current ATR / 20-bar avg ATR
    "ema_gap_atr",      # (EMA20 - EMA50) / ATR on H1
    "price_ema20_atr",  # (price - EMA20) / ATR
    # -- Candle geometry --
    "body_ratio",
    "upper_wick_pct",
    "lower_wick_pct",
    "is_engulfing",
    "is_pin_bar",
    "consec_candles",
    "m15_momentum",     # 5-bar M15 momentum / ATR
    # -- Level context --
    "level_strength",
    "level_dist_atr",
    "nearby_levels_n",
    "level_is_round",
    # -- Signal quality --
    "rr_tp1",
    "sl_dist_atr",
    # -- Direction / context --
    "direction_score",
    "htf_bias_score",
    "bias_alignment",
    "session_score",
    "hour_sin",
    "hour_cos",
    "recent_win_rate",
    # -- Indicators --
    "h4_bias_score",
    "adx_norm",
    "macd_hist_atr",
    "regime_score",
    # -- Volume --
    "vol_ratio",        # current M15 volume / 20-bar avg volume
    "vol_at_level",     # volume on level-touch candle / avg volume
    # -- External market context --
    "dxy_momentum",       # DXY 1-hour return normalised to [-1, +1]
    "us10y_level",        # US 10-year yield (%)
    "vix_level",          # VIX level (normalised: /50)
    "gvz_level",          # CBOE Gold Volatility Index (normalised: /40); >0.5 = elevated gold vol
    "tip_momentum",       # TIP ETF 1h return [-1,+1]; rising = real yields falling = gold tailwind
    # -- Trigger quality (patched at trigger time; creation placeholder = 0.5) --
    "trigger_drift_atr",  # abs(trigger_price - entry_mid) / atr_m15
    # -- MACD momentum alignment --
    "macd_fading",        # +1 = momentum fading in trade direction (good); -1 = accelerating against
    # -- Cross-engine context (injected at signal creation time) --
    "news_proximity_norm",   # 0=imminent news event, 1=safe; 10-min cache
    "equity_drawdown_pct",   # current drawdown from peak [0,1]
    "concurrent_agreement",  # +1 = same-direction signal on bus, -1 = conflicting, 0 = none
]



def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l < 1e-9:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = v * k + result * (1 - k)
    return result


def _detect_divergence(m15_candles: list[dict], rsi_vals: list[float]) -> float:
    n = min(len(m15_candles), len(rsi_vals), 10)
    if n < 5:
        return 0.0
    lows  = [float(c.get("low",  0) or 0) for c in m15_candles[-n:]]
    highs = [float(c.get("high", 0) or 0) for c in m15_candles[-n:]]
    rsiv  = rsi_vals[-n:]
    mid   = n // 2
    if min(lows[mid:]) < min(lows[:mid]) and min(rsiv[mid:]) > min(rsiv[:mid]) + 2.0:
        return 1.0
    if max(highs[mid:]) > max(highs[:mid]) and max(rsiv[mid:]) < max(rsiv[:mid]) - 2.0:
        return -1.0
    return 0.0


def _count_consecutive(m15_candles: list[dict]) -> int:
    direction = None
    count = 0
    for c in reversed(m15_candles[-10:]):
        cl = float(c.get("close", 0) or 0)
        op = float(c.get("open",  0) or 0)
        d  = 1 if cl > op else -1 if cl < op else 0
        if direction is None:
            direction = d
        if d == direction and d != 0:
            count += d
        else:
            break
    return count


def _get_recent_win_rate(n: int = 5) -> float:
    try:
        from backend.src.services.test_signal import test_signal_repo as _tdb
        recent = _tdb.get_recent_closed_signals(limit=n)
        if not recent:
            return 0.5
        wins = sum(1 for s in recent if s.get("outcome") == "win")
        return wins / len(recent)
    except Exception:
        return 0.5


def _vol_features(m15_candles: list[dict], candidate: dict, atr_m15: float) -> tuple[float, float]:
    """Returns (vol_ratio, vol_at_level)."""
    vols = [float(c.get("volume", 0) or 0) for c in m15_candles]
    if not vols or max(vols) < 1:
        return 1.0, 1.0
    avg_vol = sum(vols[-20:]) / max(len(vols[-20:]), 1)
    if avg_vol < 1:
        return 1.0, 1.0
    # Current candle volume ratio
    vol_ratio = vols[-1] / avg_vol

    # Volume on candle closest to the key level (level touch)
    level_price = float(candidate.get("key_level", 0) or 0)
    if level_price > 0 and atr_m15 > 0:
        best_idx, best_dist = -1, float("inf")
        for i, c in enumerate(m15_candles[-10:], start=len(m15_candles) - 10):
            lo = float(c.get("low",  0) or 0)
            hi = float(c.get("high", 0) or 0)
            dist = max(0.0, max(lo - level_price, level_price - hi))
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx >= 0 and best_dist < atr_m15:
            vol_at_level = vols[best_idx] / avg_vol
        else:
            vol_at_level = 1.0
    else:
        vol_at_level = vol_ratio

    return round(vol_ratio, 4), round(vol_at_level, 4)


# ── Feature extraction ────────────────────────────────────────────────────────

def extract_features(
    m15_candles: list[dict],
    h1_candles: list[dict],
    candidate: dict,
    key_levels: list[dict],
    session: str,
    htf_bias: str,
    atr_m15: float,
    *,
    h4_bias: str = "neutral",
    adx: float = 20.0,
    macd_hist: float = 0.0,
    market_ctx: Optional[dict] = None,
) -> Optional[dict]:
    if len(m15_candles) < 20 or len(h1_candles) < 55 or atr_m15 <= 0:
        return None

    m15_closes = [float(c.get("close", 0) or 0) for c in m15_candles]
    h1_closes  = [float(c.get("close", 0) or 0) for c in h1_candles]

    # RSI
    rsi_series = [_rsi(m15_closes[:i+1]) for i in range(len(m15_closes) - 20, len(m15_closes))]
    rsi_now    = rsi_series[-1] if rsi_series else 50.0
    rsi_slope  = (rsi_series[-1] - rsi_series[-3]) if len(rsi_series) >= 3 else 0.0
    rsi_div    = _detect_divergence(m15_candles, rsi_series)

    # ATR ratio
    atr_vals = []
    for i in range(max(1, len(m15_candles) - 21), len(m15_candles)):
        c, p = m15_candles[i], m15_candles[i - 1]
        hi = float(c.get("high", 0) or 0)
        lo = float(c.get("low",  0) or 0)
        pc = float(p.get("close",0) or 0)
        atr_vals.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    avg_atr   = (sum(atr_vals) / len(atr_vals)) if atr_vals else atr_m15
    atr_ratio = atr_m15 / avg_atr if avg_atr > 0 else 1.0

    # EMA features (H1) — use all available data for proper warmup
    price       = m15_closes[-1]
    ema20_h1    = _ema(h1_closes, 20)
    ema50_h1    = _ema(h1_closes, 50)
    ema_gap_atr = (ema20_h1 - ema50_h1) / atr_m15
    price_ema20 = (price - ema20_h1) / atr_m15

    # Candle geometry
    last_c = m15_candles[-1]
    prev_c = m15_candles[-2]
    c_open  = float(last_c.get("open",  0) or 0)
    c_close = float(last_c.get("close", 0) or 0)
    c_high  = float(last_c.get("high",  0) or 0)
    c_low   = float(last_c.get("low",   0) or 0)
    c_range = max(c_high - c_low, 1e-9)
    body    = abs(c_close - c_open)
    body_ratio     = body / c_range
    upper_wick     = c_high - max(c_open, c_close)
    lower_wick     = min(c_open, c_close) - c_low
    upper_wick_pct = upper_wick / c_range
    lower_wick_pct = lower_wick / c_range

    p_body_lo = min(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    p_body_hi = max(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    p_body    = p_body_hi - p_body_lo
    c_body_lo = min(c_open, c_close)
    c_body_hi = max(c_open, c_close)
    is_engulfing = 1.0 if (c_body_lo <= p_body_lo and c_body_hi >= p_body_hi
                            and body > p_body * 1.05) else 0.0

    max_wick   = max(upper_wick, lower_wick)
    is_pin_bar = 1.0 if (max_wick >= 2.0 * body and body_ratio < 0.35) else 0.0

    consec  = float(_count_consecutive(m15_candles))
    m15_mom = (m15_closes[-1] - m15_closes[-6]) / atr_m15 if len(m15_closes) >= 6 else 0.0

    # Level context
    lv_price    = float(candidate.get("key_level", price))
    lv_strength = float(candidate.get("level_strength", 1))
    lv_dist_atr = abs(lv_price - price) / atr_m15
    nearby_n    = float(sum(1 for lv in key_levels if abs(lv["price"] - price) <= atr_m15))
    lv_is_round = 1.0 if candidate.get("key_level_type") == "round" else 0.0

    # Signal quality
    rr_tp1      = float(candidate.get("rr_tp1", 0))
    sl_dist     = float(candidate.get("sl_dist", atr_m15))
    sl_dist_atr = sl_dist / atr_m15

    # Directional context
    direction       = candidate.get("direction", "BUY")
    direction_score = 1.0 if direction == "BUY" else -1.0
    htf_map         = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
    htf_score       = htf_map.get(htf_bias, 0.0)
    bias_alignment  = direction_score * htf_score
    sess_map        = {"overlap": 1.5, "london": 1.0, "ny": 1.0, "asian": 0.5, "off": 0.0}
    session_score   = sess_map.get(session, 0.0)
    hour            = datetime.now(timezone.utc).hour
    hour_sin        = math.sin(2 * math.pi * hour / 24)
    hour_cos        = math.cos(2 * math.pi * hour / 24)
    recent_wr       = _get_recent_win_rate()

    # Indicators
    h4_score     = htf_map.get(h4_bias, 0.0)
    adx_norm     = min(adx / 50.0, 2.0)
    macd_hist_atr = macd_hist / atr_m15 if atr_m15 > 0 else 0.0
    regime_val   = candidate.get("regime", "neutral")
    regime_score = {"trending": 1.0, "neutral": 0.5, "ranging": 0.0}.get(regime_val, 0.5)

    # Volume
    vol_ratio, vol_at_level = _vol_features(m15_candles, candidate, atr_m15)

    # External context
    ctx          = market_ctx or {}
    dxy_momentum = float(ctx.get("dxy_momentum", 0.0))
    us10y_level  = float(ctx.get("us10y_level",  4.5))
    vix_level    = float(ctx.get("vix_level",    20.0)) / 50.0  # normalise to ~0-1
    gvz_level    = float(ctx.get("gvz_level",    17.0)) / 40.0  # normalise to ~0-1
    tip_momentum = float(ctx.get("tip_momentum",  0.0))

    # MACD momentum alignment: +1 = momentum fading in trade direction (ideal for bounce)
    # -1 = momentum accelerating against the trade (bad for bounce)
    try:
        from backend.src.services.test_signal.signal_generator import compute_macd_hist as _cmh
        _, _hist_now  = _cmh(m15_closes)
        _, _hist_prev = _cmh(m15_closes[:-3]) if len(m15_closes) > 38 else (0.0, _hist_now)
        direction = candidate.get("direction", "BUY")
        if direction == "BUY":
            # Rising MACD = selling fading = good; falling = bad
            _macd_dir = 1.0 if _hist_now > _hist_prev else -1.0
        else:
            # Falling MACD = buying fading = good; rising = bad
            _macd_dir = 1.0 if _hist_now < _hist_prev else -1.0
        macd_fading = _macd_dir
    except Exception:
        macd_fading = 0.0

    return {
        "rsi_m15":         rsi_now,
        "rsi_slope":       rsi_slope,
        "rsi_divergence":  rsi_div,
        "atr_ratio":       atr_ratio,
        "ema_gap_atr":     ema_gap_atr,
        "price_ema20_atr": price_ema20,
        "body_ratio":      body_ratio,
        "upper_wick_pct":  upper_wick_pct,
        "lower_wick_pct":  lower_wick_pct,
        "is_engulfing":    is_engulfing,
        "is_pin_bar":      is_pin_bar,
        "consec_candles":  consec,
        "m15_momentum":    m15_mom,
        "level_strength":  lv_strength,
        "level_dist_atr":  lv_dist_atr,
        "nearby_levels_n": nearby_n,
        "level_is_round":  lv_is_round,
        "rr_tp1":          rr_tp1,
        "sl_dist_atr":     sl_dist_atr,
        "direction_score": direction_score,
        "htf_bias_score":  htf_score,
        "bias_alignment":  bias_alignment,
        "session_score":   session_score,
        "hour_sin":        hour_sin,
        "hour_cos":        hour_cos,
        "recent_win_rate": recent_wr,
        "h4_bias_score":   h4_score,
        "adx_norm":        adx_norm,
        "macd_hist_atr":   macd_hist_atr,
        "regime_score":    regime_score,
        "vol_ratio":       vol_ratio,
        "vol_at_level":    vol_at_level,
        "dxy_momentum":       dxy_momentum,
        "us10y_level":        us10y_level,
        "vix_level":          vix_level,
        "gvz_level":          gvz_level,
        "tip_momentum":       tip_momentum,
        # Placeholder — patched with actual value when signal triggers
        "trigger_drift_atr":  0.5,
        "macd_fading":        macd_fading,
        # Cross-engine context — injected into candidate by engine.py before calling extract_features
        "news_proximity_norm":  float(candidate.get("news_proximity_norm",  1.0)),
        "equity_drawdown_pct":  float(candidate.get("equity_drawdown_pct",  0.0)),
        "concurrent_agreement": float(candidate.get("concurrent_agreement", 0.0)),
    }


def to_vector(features: dict) -> list[float]:
    return [float(features.get(k, 0.0)) for k in FEATURE_NAMES]


