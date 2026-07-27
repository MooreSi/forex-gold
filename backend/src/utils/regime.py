"""
Shared day-type / regime classification for the signal generator engines.

Built from measured per-day performance (279 bounce + 231 breakout closed
signals, 2026-06-15 → 2026-07-06) correlated against MT5 XAUUSD daily data:

  - Bounce made money ONLY on low-efficiency chop days
    (Jun 29 eff=0.09 → +$61; Jun 30 eff=0.13 → +$123) and lost heavily on
    directional days (Jul 1 eff=0.79 → -$305; Jul 6 grind down → -$586).
  - Breakout made money ONLY at very strong trend (ADX 50+ bucket: +$98;
    every bucket below 50 negative) and outside the 12-14 UTC window
    (-$1,079 inside, +$126 outside).
  - The "mushy middle" (moderate ADX, moderate efficiency) lost for BOTH
    engines — most of the day-to-day inconsistency came from trading it.

All functions are pure and stateless — safe to share between the otherwise
isolated engines without coupling their data.
"""
from __future__ import annotations

# ── Per-engine hour blocks (UTC) ─────────────────────────────────────────────
# Measured cumulative PnL by signal-creation hour (Mac node, all history):
#   bounce:   07h -$358 | 12h -$124 | 13h -$228 | 14h -$139 | 15h -$141 |
#             16h -$181 | 20h -$206   → combined -$1,377 across 7 hours
#   breakout: 12h -$351 | 13h -$457 | 14h -$272 → combined -$1,079
# London morning (08-11) and Asian hours carried the profitable trades for
# both engines and stay open.
BOUNCE_BLOCKED_HOURS_UTC:   frozenset[int] = frozenset({7, 12, 13, 14, 15, 16, 20})
BREAKOUT_BLOCKED_HOURS_UTC: frozenset[int] = frozenset({12, 13, 14})

# ── Level-type filters ───────────────────────────────────────────────────────
# Bounce entries AT these level types lost consistently (they break more often
# than they hold — internally consistent with the breakout engine's data where
# BREAKS of liq_low were its single best trade at +$387/69% WR):
#   liq_low -$744 (37% WR) | fvg_bear -$515 | fvg_bull -$320 | liq_high -$106
# They remain valid as TP targets and for breakout trading — only bounce
# ENTRIES at them are blocked.
BOUNCE_BLOCKED_ENTRY_LEVELS: frozenset[str] = frozenset(
    {"liq_low", "liq_high", "fvg_bull", "fvg_bear"}
)

# Breakouts THROUGH these level types lost consistently:
#   fvg_bull -$793 (37% WR) | round -$223 (36% WR)
BREAKOUT_BLOCKED_LEVELS: frozenset[str] = frozenset({"fvg_bull", "round"})


def efficiency_ratio(closes: list[float], n: int = 24) -> float:
    """
    Kaufman Efficiency Ratio over the last `n` closes:
    |net change| / sum(|bar-to-bar changes|).  1.0 = perfectly directional,
    0.0 = pure chop.  Catches steady grinds (like Jul 6) that a same-timeframe
    ADX under-reads because they never accelerate.
    """
    if len(closes) < n + 1:
        return 0.5  # neutral when insufficient data
    window = closes[-(n + 1):]
    net = abs(window[-1] - window[0])
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if path < 1e-9:
        return 0.0
    return round(net / path, 3)


def classify_day(m15_adx: float, h1_eff: float) -> str:
    """
    Five-state day classification.

      strong_trend  — M15 ADX >= 40 and H1 efficiency >= 0.40: persistent
                      directional day (Jul 6). Mean reversion is unsafe in
                      either direction; breakout continuation viable.
      trend         — ADX >= 28 and efficiency >= 0.30: directional but not
                      extreme. Only high-conviction setups either way.
      range         — ADX < 22 and efficiency < 0.25: chop (Jun 29/30).
                      Bounce's home regime; breakout should stand down.
      volatile_chop — ADX >= 28 but efficiency < 0.20: violent two-way days
                      (V-reversals like Jun 24, bounce -$308). Hostile to both.
      neutral       — everything else: the mushy middle where both engines
                      historically bled. Trade minimally.
    """
    if m15_adx >= 40 and h1_eff >= 0.40:
        return "strong_trend"
    if m15_adx >= 28 and h1_eff < 0.20:
        return "volatile_chop"
    if m15_adx >= 28 and h1_eff >= 0.30:
        return "trend"
    if m15_adx < 22 and h1_eff < 0.25:
        return "range"
    return "neutral"


def bounce_pattern_allowed(pattern: str, day_type: str) -> tuple[bool, str]:
    """
    Pattern × regime permission matrix for the bounce engine, from measured
    per-pattern expectancy:

      engulfing  75% WR +$466 | pin_bar 75% WR +$216   → allowed everywhere
                                                          except strong_trend
      liquidity_sweep 57% WR -$683                      → range/neutral only
      bounce (2-candle)  48% WR -$678                   → range only
      rsi_divergence 14% WR -$330                       → removed at source

    Returns (allowed, reason_if_blocked).
    """
    if pattern in ("engulfing", "pin_bar"):
        if day_type == "strong_trend":
            return False, f"{pattern} blocked in strong_trend day"
        return True, ""
    if pattern == "liquidity_sweep":
        if day_type in ("range", "neutral"):
            return True, ""
        return False, f"liquidity_sweep blocked in {day_type} day (range/neutral only)"
    if pattern == "bounce":
        if day_type == "range":
            return True, ""
        return False, f"bounce pattern blocked in {day_type} day (range only)"
    if pattern == "scalp":
        # M5 scalp trigger already restricted to open windows + strong patterns
        if day_type == "strong_trend":
            return False, "scalp blocked in strong_trend day"
        return True, ""
    return True, ""
