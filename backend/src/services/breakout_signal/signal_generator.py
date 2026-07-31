"""
Breakout / trend-following signal generator for XAUUSD.

Two entry types:
  • break-and-go   — enter on the candle that CLOSES beyond a key level
                     with momentum confirmation (MACD growing, bias aligned)
  • break-and-retest — price previously broke through a level and has now
                       pulled back to retest it; enter on bounce from the level

Shared utilities (HTF bias, ADX, MACD, key levels, session) are imported
from the existing test_signal.signal_generator to avoid duplication.
No state is modified here — pure analysis functions only.
"""
from __future__ import annotations

import math
from typing import Optional

from backend.src.services.breakout_signal import adaptive_params as ap

# ── Re-export shared utilities from the bounce engine (read-only) ──────────────
# These are pure functions with no side effects. Importing is safe and DRY.
from backend.src.services.test_signal.signal_generator import (
    compute_htf_bias,
    compute_h4_bias,
    compute_adx,
    compute_macd_hist,
    detect_regime,
    identify_key_levels,
    get_session,
    session_quality,
    session_is_active,
    is_news_window,
)


# ── Breakout detection ────────────────────────────────────────────────────────

def _candle_body_close(candle: dict) -> float:
    """Return the closing price of the candle body (= the close)."""
    return float(candle.get("close", 0) or 0)


def _body_beyond_level(candle: dict, level: float, direction: str) -> float:
    """
    How many points the candle BODY (open→close) extends beyond a level.
    Returns 0.0 if the body has not crossed the level.
    """
    o = float(candle.get("open",  0) or 0)
    c = float(candle.get("close", 0) or 0)
    body_lo = min(o, c)
    body_hi = max(o, c)
    if direction == "BUY":
        # Body must be closing above resistance
        return max(0.0, c - level) if c > level and body_hi > level else 0.0
    else:
        # Body must be closing below support
        return max(0.0, level - c) if c < level and body_lo < level else 0.0


def _is_compressed(candles: list[dict], atr: float, max_range_atr: float,
                   window: int = 12) -> bool:
    """
    True when the `window` candles show volatility contraction: their combined
    high-low span is at most max_range_atr × ATR. A genuine breakout emerges
    from compression (squeeze → expansion); a 'break' appearing mid-run in an
    already-extended move is continuation-chasing, the pattern behind most of
    this engine's recorded losses.
    """
    if atr <= 0 or len(candles) < window:
        return False
    span_hi = max(float(c.get("high", 0) or 0) for c in candles[-window:])
    span_lo = min(float(c.get("low", 0) or 0) for c in candles[-window:])
    if span_hi <= 0 or span_lo <= 0:
        return False
    return (span_hi - span_lo) <= max_range_atr * atr


def _adx_rising(candles: list[dict], adx_now: float, lookback: int = 3) -> bool:
    """ADX now vs `lookback` candles ago — rising ADX from a moderate base is
    the signature of a fresh trend leg; a break on falling ADX tends to fade."""
    if len(candles) < 30 + lookback:
        return True   # not enough history to judge — don't block
    try:
        adx_prev = compute_adx(candles[-(30 + lookback):-lookback])
    except Exception:
        return True
    return adx_now > adx_prev + 0.25


def _strong_close(candle: dict, direction: str) -> bool:
    """Breakout candle must CLOSE near its extreme in the break direction
    (outer 35% of its range) — a long rejection wick back through the level
    is a failed break, not a strong one."""
    hi = float(candle.get("high", 0) or 0)
    lo = float(candle.get("low", 0) or 0)
    c  = float(candle.get("close", 0) or 0)
    rng = hi - lo
    if rng <= 0:
        return False
    pos = (c - lo) / rng
    return pos >= 0.65 if direction == "BUY" else pos <= 0.35


def check_breakout_go(
    m15_candles: list[dict],
    key_levels: list[dict],
    htf_bias: str,
    h4_bias: str,
    current_price: float,
    atr: float,
    adx: float,
    macd_hist: float,
) -> Optional[dict]:
    """
    Detect a break-and-go entry: the last closed M15 candle body closed through
    a key level with momentum confirmation and bias alignment.

    2026-07-16 rebuild — a valid break-and-go now additionally requires:
      • ADX below the exhaustion cap AND rising (fresh leg, not a mature one)
      • volatility compression in the candles BEFORE the break (squeeze fuel)
      • the breakout candle to close strongly in the break direction

    Returns a candidate dict or None.
    """
    if len(m15_candles) < 15:
        return None

    min_adx       = ap.get("min_adx_go")
    max_adx       = ap.get("max_adx_entry")
    min_break_pts = ap.get("min_break_pts")
    dual_bias     = ap.get("require_dual_bias") >= 0.5
    # Volatility-scaled break floor — a fixed point threshold is meaningless across
    # gold's wide ATR range (5pts in dead Asian hours vs 30pts+ in NY news spikes).
    min_break_pts = max(min_break_pts, atr * ap.get("min_break_atr_frac"))
    exhaustion_cap = atr * ap.get("exhaustion_atr_mult")
    min_strength  = ap.get("min_level_strength")

    if adx < min_adx or adx > max_adx:
        return None

    if ap.get("require_adx_rising") >= 0.5 and not _adx_rising(m15_candles, adx):
        return None

    # Compression before the break — measured on the candles preceding the
    # breakout candle itself (the break candle's own expansion must not count
    # toward the consolidation span it is escaping from).
    if not _is_compressed(m15_candles[:-1], atr, ap.get("compression_max_range_atr")):
        return None

    last = m15_candles[-1]   # most recently closed candle
    prev = m15_candles[-2]   # candle before that

    # Anti-exhaustion guard: a breakout candle far larger than ATR is usually a
    # climactic stop-hunt/liquidity-grab spike, not the start of a sustained leg —
    # these are the entries most likely to spike through a level and immediately
    # mean-revert. Skip break-and-go entirely on outsized candles.
    last_body_full = abs(
        float(last.get("close", 0) or 0) - float(last.get("open", 0) or 0)
    )
    if last_body_full > exhaustion_cap:
        return None

    for level_info in key_levels:
        if int(level_info.get("strength", 1)) < min_strength:
            continue
        level     = float(level_info.get("price", 0) or 0)
        ltype     = level_info.get("type", "unknown")
        strength  = int(level_info.get("strength", 1))
        if level <= 0:
            continue

        # ── BUY breakout: body closes above resistance ────────────────────────
        if (htf_bias in ("bullish", "neutral") and
                (not dual_bias or h4_bias in ("bullish", "neutral"))):
            # Last candle body closed above the level
            pts_above = _body_beyond_level(last, level, "BUY")
            if pts_above >= min_break_pts:
                # Previous candle was AT or BELOW the level (fresh break)
                prev_close = _candle_body_close(prev)
                if prev_close <= level + atr * 0.1:
                    # MACD must be positive (momentum confirmation), the breakout
                    # candle body substantial (>= 35% of ATR), and the candle must
                    # close strongly toward the break (no big rejection wick).
                    last_body = abs(
                        float(last.get("close", 0) or 0) - float(last.get("open", 0) or 0)
                    )
                    if (macd_hist > 0 and last_body >= atr * 0.35
                            and _strong_close(last, "BUY")):
                        return {
                            "direction":        "BUY",
                            "breakout_type":    "go",
                            "broken_level":     level,
                            "broken_level_type": ltype,
                            "level_strength":   strength,
                            "pts_beyond":       round(pts_above, 2),
                        }

        # ── SELL breakout: body closes below support ──────────────────────────
        if (htf_bias in ("bearish", "neutral") and
                (not dual_bias or h4_bias in ("bearish", "neutral"))):
            pts_below = _body_beyond_level(last, level, "SELL")
            if pts_below >= min_break_pts:
                prev_close = _candle_body_close(prev)
                if prev_close >= level - atr * 0.1:
                    last_body = abs(
                        float(last.get("close", 0) or 0) - float(last.get("open", 0) or 0)
                    )
                    if (macd_hist < 0 and last_body >= atr * 0.35
                            and _strong_close(last, "SELL")):
                        return {
                            "direction":        "SELL",
                            "breakout_type":    "go",
                            "broken_level":     level,
                            "broken_level_type": ltype,
                            "level_strength":   strength,
                            "pts_beyond":       round(pts_below, 2),
                        }
    return None


def check_breakout_retest(
    m15_candles: list[dict],
    key_levels: list[dict],
    htf_bias: str,
    h4_bias: str,
    current_price: float,
    atr: float,
    adx: float,
    macd_hist: float,
) -> Optional[dict]:
    """
    Detect a break-and-retest entry:
      1. Within the last N candles, price broke through a key level with an
         IMPULSIVE candle (body >= 0.5 ATR — a weak drift through a level is
         not a break worth retesting)
      2. Price has pulled back to the level for the FIRST time since the break
         (first pullback carries the edge; second and third touches erode it)
      3. The last CLOSED candle confirms rejection: it wicked into the level
         and closed back beyond it, in the break direction, near its own high
         (BUY) / low (SELL)

    2026-07-16 rebuild: the old version entered while price was still sitting
    AT the level with no rejection close — catching the knife on every deep
    pullback that kept going. 257 recorded retest trades, 45.9% WR, -$1,657.

    Returns a candidate dict or None.
    """
    if len(m15_candles) < 5:
        return None

    min_adx      = ap.get("min_adx_retest")
    max_adx      = ap.get("max_adx_entry")
    tolerance    = ap.get("retest_atr_tolerance") * atr
    lookback     = int(ap.get("lookback_candles"))
    min_break    = max(ap.get("min_break_pts"), atr * ap.get("min_break_atr_frac"))
    dual_bias    = ap.get("require_dual_bias") >= 0.5
    min_strength = ap.get("min_level_strength")
    max_touches  = int(ap.get("retest_max_prior_touches"))

    if adx < min_adx or adx > max_adx:
        return None

    last = m15_candles[-1]

    for level_info in key_levels:
        level    = float(level_info.get("price", 0) or 0)
        ltype    = level_info.get("type", "unknown")
        strength = int(level_info.get("strength", 1))
        if level <= 0 or strength < min_strength:
            continue

        # Look for a prior BUY break in lookback window (excluding last 1 candle)
        scan_window = m15_candles[-(lookback + 1):-1]
        if not scan_window:
            continue

        # ── BUY retest ───────────────────────────────────────────────────────
        if (htf_bias in ("bullish", "neutral") and
                (not dual_bias or h4_bias in ("bullish", "neutral"))):

            # 1. Impulsive prior break: candle body closed above the level by
            #    min_break AND the break candle itself was substantial.
            break_idx = None
            for i, c in enumerate(scan_window):
                body = abs(float(c.get("close", 0) or 0) - float(c.get("open", 0) or 0))
                if (_body_beyond_level(c, level, "BUY") >= min_break
                        and body >= atr * 0.5):
                    break_idx = i   # keep last (most recent) qualifying break
            if break_idx is not None:
                # 2. First-pullback-only: count how many candles AFTER the break
                #    (excluding the current rejection candle) already dipped into
                #    the retest zone. Each prior touch consumes the resting
                #    orders that make the level hold.
                after_break = scan_window[break_idx + 1:]
                prior_touches = sum(
                    1 for c in after_break
                    if float(c.get("low", level + 999) or (level + 999)) <= level + tolerance
                )
                # 3. Rejection confirmation on the last CLOSED candle: wick into
                #    the zone, bullish body, close back above the level and near
                #    the candle high.
                last_low   = float(last.get("low",  current_price) or current_price)
                last_open  = float(last.get("open", 0) or 0)
                last_close = _candle_body_close(last)
                rejection = (
                    last_low <= level + tolerance          # wicked into the zone
                    and last_close > level                 # closed back above it
                    and last_close > last_open             # bullish candle
                    and _strong_close(last, "BUY")         # close near its high
                )
                near_level = abs(current_price - level) <= tolerance * 1.5
                if prior_touches < max_touches and rejection and near_level:
                    return {
                        "direction":        "BUY",
                        "breakout_type":    "retest",
                        "broken_level":     level,
                        "broken_level_type": ltype,
                        "level_strength":   strength,
                        "prior_break_candles": 1,
                    }

        # ── SELL retest ──────────────────────────────────────────────────────
        if (htf_bias in ("bearish", "neutral") and
                (not dual_bias or h4_bias in ("bearish", "neutral"))):

            break_idx = None
            for i, c in enumerate(scan_window):
                body = abs(float(c.get("close", 0) or 0) - float(c.get("open", 0) or 0))
                if (_body_beyond_level(c, level, "SELL") >= min_break
                        and body >= atr * 0.5):
                    break_idx = i
            if break_idx is not None:
                after_break = scan_window[break_idx + 1:]
                prior_touches = sum(
                    1 for c in after_break
                    if float(c.get("high", level - 999) or (level - 999)) >= level - tolerance
                )
                last_high  = float(last.get("high", current_price) or current_price)
                last_open  = float(last.get("open", 0) or 0)
                last_close = _candle_body_close(last)
                rejection = (
                    last_high >= level - tolerance
                    and last_close < level
                    and last_close < last_open
                    and _strong_close(last, "SELL")
                )
                near_level = abs(current_price - level) <= tolerance * 1.5
                if prior_touches < max_touches and rejection and near_level:
                    return {
                        "direction":        "SELL",
                        "breakout_type":    "retest",
                        "broken_level":     level,
                        "broken_level_type": ltype,
                        "level_strength":   strength,
                        "prior_break_candles": 1,
                    }
    return None


# ── Liquidity-sweep reversal ──────────────────────────────────────────────────

def check_liquidity_sweep(
    m5_candles: list[dict],
    key_levels: list[dict],
    htf_bias: str,
    h4_bias: str,
    current_price: float,
    atr: float,
    adx: float,
    macd_hist: float,
) -> Optional[dict]:
    """
    Detect a liquidity-sweep reversal: the last closed candle WICKS through a
    key level (running the stops/liquidity resting beyond it) but CLOSES back
    inside, rejecting the break. Enter in the reversal direction with the stop
    beyond the sweep wick.

    This is the mirror image of the failed break-and-go: the spike through the
    level that mean-reverts — the exact pattern that produced most breakout
    losses in the 12:00-15:00 UTC window — becomes the entry instead of the trap.

    Returns a candidate dict (breakout_type="sweep") or None.
    """
    if len(m5_candles) < 3 or atr <= 0:
        return None

    # Sweeps are reversal plays — a very strong trend (high ADX) keeps running
    # through swept levels instead of reversing, so cap rather than floor ADX.
    if adx > ap.get("max_adx_sweep"):
        return None

    min_strength = ap.get("min_level_strength")
    dual_bias    = ap.get("require_dual_bias") >= 0.5
    min_pierce   = max(ap.get("min_break_pts"), atr * ap.get("sweep_wick_atr_frac"))
    close_buf    = atr * 0.05        # close must be back inside by this margin

    last  = m5_candles[-1]
    o     = float(last.get("open",  0) or 0)
    c     = float(last.get("close", 0) or 0)
    hi    = float(last.get("high",  0) or 0)
    lo    = float(last.get("low",   0) or 0)
    if not (o and c and hi and lo):
        return None

    for level_info in key_levels:
        level    = float(level_info.get("price", 0) or 0)
        ltype    = level_info.get("type", "unknown")
        strength = int(level_info.get("strength", 1))
        if level <= 0 or strength < min_strength:
            continue

        # ── SELL sweep: wick ran the highs above resistance, closed back below ─
        if (htf_bias in ("bearish", "neutral") and
                (not dual_bias or h4_bias in ("bearish", "neutral"))):
            pierce = hi - level
            if (pierce >= min_pierce
                    and c < level - close_buf     # closed back inside
                    and c < o                      # rejection candle is bearish
                    and abs(current_price - level) <= atr * 0.5):  # still near the level
                return {
                    "direction":         "SELL",
                    "breakout_type":     "sweep",
                    "broken_level":      level,
                    "broken_level_type": ltype,
                    "level_strength":    strength,
                    "wick_extreme":      hi,
                    "pierce_pts":        round(pierce, 2),
                }

        # ── BUY sweep: wick ran the lows below support, closed back above ──────
        if (htf_bias in ("bullish", "neutral") and
                (not dual_bias or h4_bias in ("bullish", "neutral"))):
            pierce = level - lo
            if (pierce >= min_pierce
                    and c > level + close_buf
                    and c > o
                    and abs(current_price - level) <= atr * 0.5):
                return {
                    "direction":         "BUY",
                    "breakout_type":     "sweep",
                    "broken_level":      level,
                    "broken_level_type": ltype,
                    "level_strength":    strength,
                    "wick_extreme":      lo,
                    "pierce_pts":        round(pierce, 2),
                }
    return None


# ── Risk levels ───────────────────────────────────────────────────────────────

def calculate_breakout_risk_levels(
    candidate: dict,
    current_price: float,
    atr: float,
    adx: float,
) -> Optional[dict]:
    """
    Calculate SL and TPs for a breakout signal.

    SL: just beyond the broken level + ATR buffer
    TPs: scaled for trend-following (TP1 = 1.5× SL dist, TP2 = 3×, TP3 = 5×)

    Returns None if R:R is unacceptable.
    """
    direction  = candidate["direction"]
    level      = candidate["broken_level"]
    sl_buf     = ap.get("sl_atr_mult") * atr
    entry      = current_price

    # Sweep entries stop out beyond the sweep WICK, not the level: the wick
    # already marks where the liquidity grab exhausted, so a revisit of that
    # extreme invalidates the reversal. Tighter buffer than break entries.
    if candidate.get("breakout_type") == "sweep":
        wick   = float(candidate.get("wick_extreme", level))
        sl_buf = ap.get("sweep_sl_atr_mult") * atr
        if direction == "BUY":
            sl      = wick - sl_buf
            sl_dist = entry - sl
        else:
            sl      = wick + sl_buf
            sl_dist = sl - entry
    elif direction == "BUY":
        sl      = level - sl_buf
        sl_dist = entry - sl
    else:
        sl      = level + sl_buf
        sl_dist = sl - entry

    if sl_dist <= 0:
        return None

    # 2026-07-16 rebuild: enforce a volatility floor and cap on the stop.
    # Recorded average SL distance was 0.61×ATR and 85% of losses stopped out
    # within 15 minutes — noise stops. A stop that can't survive one ordinary
    # M5 candle isn't a stop, it's a donation. Floor pushes the SL out to at
    # least min_sl_dist_atr × ATR from entry (still beyond the structure);
    # cap rejects entries so far from the level that the structure-anchored
    # stop implies chasing (> max_sl_dist_atr × ATR).
    min_dist = ap.get("min_sl_dist_atr") * atr
    max_dist = ap.get("max_sl_dist_atr") * atr
    if sl_dist > max_dist:
        return None
    if sl_dist < min_dist:
        sl_dist = min_dist
        sl = entry - min_dist if direction == "BUY" else entry + min_dist

    # TP multipliers are adaptive-tunable (default 1.0 / 1.8 / 2.8). The original
    # fixed 1.5/3.0/5.0 scheme made the R:R check a tautology (TP1 was ALWAYS
    # exactly 1.5R, so "min R:R 1.2" never filtered anything) and set TP3 far
    # beyond what gold M5 breakouts actually sustain — live data showed realized
    # wins averaging ~1R because price reverses through the trailed stop long
    # before reaching 3-5R. Tighter, partial-close-backed targets bank real
    # profit instead of giving it back on the trail back to TP1.
    tp1_mult = ap.get("tp1_mult")
    tp2_mult = ap.get("tp2_mult")
    tp3_mult = ap.get("tp3_mult")

    tp1 = entry + sl_dist * tp1_mult if direction == "BUY" else entry - sl_dist * tp1_mult
    tp2 = entry + sl_dist * tp2_mult if direction == "BUY" else entry - sl_dist * tp2_mult
    tp3 = entry + sl_dist * tp3_mult if direction == "BUY" else entry - sl_dist * tp3_mult
    rr  = round(abs(tp1 - entry) / sl_dist, 2)

    # Minimum R:R = 0.85 (TP1 may now sit slightly under 1R since it's a partial
    # take-profit, not the only exit — TP2/TP3 carry the remainder).
    if rr < 0.85:
        return None

    return {
        "entry_mid": round(entry, 2),
        "stop_loss": round(sl, 2),
        "tp1":       round(tp1, 2),
        "tp2":       round(tp2, 2),
        "tp3":       round(tp3, 2),
        "sl_dist":   round(sl_dist, 2),
        "rr_tp1":    rr,
    }
