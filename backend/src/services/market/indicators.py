"""Higher-timeframe indicators: H4 bias, ADX, MACD histogram, regime.

Moved out of the Bounce engine when that engine was removed (2026-09-14).
Pure maths: candles or closes in, a number or a word out.

It was never Bounce-specific -- the **Breakout** engine imports all four of
these and always did, while they lived in another engine's package.

What did NOT come across is the part that read the Bounce engine's own
adaptive parameters -- `calculate_risk_levels`, `_counter_bias_allowed`,
`check_scalp_trigger`, `calculate_scalp_risk_levels`. Those were the engine,
and they went with it.
"""
from __future__ import annotations

from backend.src.services.market.levels import _ema


# ── H4 bias ───────────────────────────────────────────────────────────────────

def compute_h4_bias(h4_candles: list[dict]) -> str:
    """H4 HTF bias using EMA20/50 with full data warmup — same logic as H1."""
    if len(h4_candles) < 52:
        return "neutral"
    closes = [float(c["close"]) for c in h4_candles if c.get("close")]
    if len(closes) < 52:
        return "neutral"
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    price = closes[-1]
    if price > ema20 > ema50:
        return "bullish"
    if price < ema20 < ema50:
        return "bearish"
    return "neutral"


# ── ADX ───────────────────────────────────────────────────────────────────────

def compute_adx(candles: list[dict], period: int = 14) -> float:
    """
    ADX using Wilder's smoothing.  Returns 0-100.
    >25 = trending, <20 = ranging.
    """
    if len(candles) < period + 2:
        return 20.0

    highs  = [float(c.get("high",  0) or 0) for c in candles]
    lows   = [float(c.get("low",   0) or 0) for c in candles]
    closes = [float(c.get("close", 0) or 0) for c in candles]

    tr_list: list[float] = []
    dm_plus: list[float] = []
    dm_minus: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up   = highs[i]      - highs[i - 1]
        down = lows[i - 1]   - lows[i]
        dm_plus.append(up   if up   > down and up   > 0 else 0.0)
        dm_minus.append(down if down > up   and down > 0 else 0.0)
        tr_list.append(tr)

    if len(tr_list) < period:
        return 20.0

    alpha = 1.0 / period

    def _wilder(vals: list[float]) -> list[float]:
        result = [sum(vals[:period])]
        for v in vals[period:]:
            result.append(result[-1] * (1 - alpha) + v)
        return result

    atr14 = _wilder(tr_list)
    pdm14 = _wilder(dm_plus)
    mdm14 = _wilder(dm_minus)

    dx_list: list[float] = []
    for a, p, m in zip(atr14, pdm14, mdm14):
        if a < 1e-9:
            continue
        pdi   = 100 * p / a
        mdi   = 100 * m / a
        denom = pdi + mdi
        dx_list.append(100 * abs(pdi - mdi) / denom if denom > 1e-9 else 0.0)

    if not dx_list:
        return 20.0
    return round(sum(dx_list[-period:]) / min(len(dx_list), period), 2)


# ── MACD histogram ────────────────────────────────────────────────────────────

def compute_macd_hist(closes: list[float]) -> tuple[float, float]:
    """
    MACD(12, 26, 9).  Returns (macd_line, histogram).
    Requires at least 35 closes.
    """
    if len(closes) < 35:
        return 0.0, 0.0
    n = len(closes)
    macd_vals: list[float] = []
    for end in range(n - 8, n + 1):
        if end < 26:
            macd_vals.append(0.0)
            continue
        seg = closes[max(0, end - 50):end]
        macd_vals.append(_ema(seg[-26:], 12) - _ema(seg[-26:], 26))
    macd_line   = macd_vals[-1]
    signal_line = _ema(macd_vals[-9:], 9) if len(macd_vals) >= 9 else macd_line
    return round(macd_line, 4), round(macd_line - signal_line, 4)


# ── Market regime ─────────────────────────────────────────────────────────────

def detect_regime(adx: float, h1_bias: str, h4_bias: str) -> str:
    """
    trending — ADX > 25 AND H1 and H4 agree on a non-neutral direction.
    ranging  — ADX < 20.
    neutral  — everything else.
    """
    if adx > 25 and h1_bias == h4_bias and h1_bias != "neutral":
        return "trending"
    if adx < 20:
        return "ranging"
    return "neutral"
