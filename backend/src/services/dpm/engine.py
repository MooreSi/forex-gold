"""
Adaptive Dynamic Position Management engine.

All parameters (trail distance, BE trigger, TP1 close %) are computed from
current market data — ATR, ADX regime, session, momentum, and structural
swing levels.

Self-calibration: after enough closed DPM trades, `run_calibration` derives
optimal be_multiplier and trail_multiplier per (session, momentum_bucket)
from actual outcomes.  Calibrated values are stored in app_config and used
at runtime in preference to the hardcoded defaults.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Hard bounds for XAUUSD (safety rails only)
_MIN_TRAIL = 2.0
_MAX_TRAIL = 50.0
_MIN_BE    = 2.0
_MAX_BE    = 30.0

# Session multipliers: wider trail in volatile sessions, tighter in quiet ones
_SESSION_MULT = {
    "asian":   0.80,
    "london":  1.20,
    "ny":      1.10,
    "overlap": 1.40,   # London/NY crossover — most volatile window
}

# Regime-specific adjustments applied on top of session multipliers.
# trending:    let runners run with a wider trail and smaller TP1 partial.
# weak_trend:  ADX 20-25 — intermediate; some directional bias but not committed.
# ranging:     bank profit quickly with a tight trail and large TP1 partial.
# spike:       protect quickly — very tight trail and early partial close.
_REGIME_CONFIG = {
    "trending":   {"trail_mult": 1.40, "be_mult": 1.20, "tp1_pct": 0.25},
    "weak_trend": {"trail_mult": 1.10, "be_mult": 1.05, "tp1_pct": 0.40},
    "ranging":    {"trail_mult": 0.85, "be_mult": 0.90, "tp1_pct": 0.60},
    "spike":      {"trail_mult": 0.70, "be_mult": 0.70, "tp1_pct": 0.50},
}

# Minimum trades required per (session, momentum) group before calibration
# values are trusted and applied.
_MIN_CAL_SAMPLES = 5


# ── Market analysis ───────────────────────────────────────────────────────────

def compute_atr(candles: list[dict], period: int = 14) -> float:
    """Wilder ATR over `period` candles using exponential smoothing. Returns price value ($)."""
    if len(candles) < 2:
        return 10.0

    trs: list[float] = []
    for i in range(1, len(candles)):
        h  = float(candles[i].get("high",  0) or 0)
        lo = float(candles[i].get("low",   0) or 0)
        cp = float(candles[i - 1].get("close", 0) or 0)
        if not (h and lo and cp):
            continue
        trs.append(max(h - lo, abs(h - cp), abs(lo - cp)))

    if not trs:
        return 10.0

    # Wilder's smoothing: seed with SMA of first `period` bars, then
    # ATR[n] = (ATR[n-1] * (period-1) + TR[n]) / period
    window = trs[-max(period * 2, len(trs)):] if len(trs) > period else trs
    seed   = sum(window[:period]) / period if len(window) >= period else sum(window) / len(window)
    atr    = seed
    for tr in window[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(max(atr, 0.5), 2)


def compute_adx(candles: list[dict], period: int = 14) -> float:
    """
    Wilder ADX (Average Directional Index) over `period` candles.
    Returns 0–100; > 25 = trending, < 20 = ranging.
    """
    if len(candles) < period + 2:
        return 20.0

    plus_dm_list:  list[float] = []
    minus_dm_list: list[float] = []
    tr_list:       list[float] = []

    for i in range(1, len(candles)):
        h   = float(candles[i].get("high",  0) or 0)
        lo  = float(candles[i].get("low",   0) or 0)
        h_p = float(candles[i - 1].get("high",  0) or 0)
        lo_p = float(candles[i - 1].get("low",  0) or 0)
        cp  = float(candles[i - 1].get("close", 0) or 0)
        if not (h and lo and h_p and lo_p and cp):
            continue
        up_move   = h - h_p
        down_move = lo_p - lo
        plus_dm_list.append(up_move   if (up_move > down_move and up_move > 0)   else 0.0)
        minus_dm_list.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr_list.append(max(h - lo, abs(h - cp), abs(lo - cp)))

    if len(tr_list) < period:
        return 20.0

    def _wilder_sum(values: list[float], n: int) -> list[float]:
        # Wilder smoothed SUM form used for TR, +DM, -DM.
        # Initialises with the raw sum; fixed point is n × average_value.
        # When used in a ratio (DI+ = smoothed_+DM / smoothed_TR) the n cancels.
        s = [sum(values[:n])]
        for v in values[n:]:
            s.append(s[-1] - s[-1] / n + v)
        return s

    def _wilder_avg(values: list[float], n: int) -> list[float]:
        # Wilder smoothed AVERAGE form used for DX → ADX.
        # Initialises with the simple average; each step is (prev*(n-1)+v)/n.
        # Fixed point equals the long-run average of the input (stays in 0-100
        # when the input is 0-100, unlike the sum form which inflates by n×).
        s = [sum(values[:n]) / n]
        for v in values[n:]:
            s.append((s[-1] * (n - 1) + v) / n)
        return s

    atr_s = _wilder_sum(tr_list,       period)
    pdm_s = _wilder_sum(plus_dm_list,  period)
    mdm_s = _wilder_sum(minus_dm_list, period)

    di_plus  = [100 * p / a if a > 0 else 0.0 for p, a in zip(pdm_s, atr_s)]
    di_minus = [100 * m / a if a > 0 else 0.0 for m, a in zip(mdm_s, atr_s)]

    dx_list: list[float] = []
    for p, m in zip(di_plus, di_minus):
        total = p + m
        dx_list.append(100 * abs(p - m) / total if total > 0 else 0.0)

    if len(dx_list) < period:
        return 20.0

    # Use the bounded average form for ADX so it stays within 0-100
    adx_s = _wilder_avg(dx_list, period)
    return round(adx_s[-1], 1) if adx_s else 20.0


def detect_session() -> str:
    """Return current session based on UTC hour."""
    h = datetime.now(timezone.utc).hour
    london = 7  <= h < 16
    ny     = 12 <= h < 21
    if london and ny:
        return "overlap"
    if london:
        return "london"
    if ny:
        return "ny"
    return "asian"


def is_weekly_market_closed(now: datetime | None = None) -> bool:
    """Whether the forex market is closed for the weekend right now.

    detect_session() above only checks hour-of-day and assumes every day
    has trading hours — it has no idea Saturday/Sunday exist, so during the
    weekend it still confidently reports "asian"/"ny"/etc based on the raw
    clock. That's wrong: the whole market is shut Fri ~21:00 UTC through
    Sun ~22:00 UTC (standard forex week, hardcoded rather than queried live
    from the broker so this still works even when the MT5 bridge itself is
    down — which is exactly when callers most need an accurate answer).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    wd = now.weekday()  # Monday=0 ... Sunday=6
    if wd == 5:                      # Saturday: closed all day
        return True
    if wd == 6:                      # Sunday: closed until reopen
        return now.hour < 22
    if wd == 4:                      # Friday: closed after close
        return now.hour >= 21
    return False


def detect_regime(candles: list[dict], atr: float) -> str:
    """
    Classify market regime as 'trending', 'weak_trend', 'ranging', or 'spike'.

    - spike       : current ATR > 1.8× recent average bar range (intraday volatility event)
    - trending    : ADX > 25  — committed directional move
    - weak_trend  : ADX 20-25 — directional bias present but not strong
    - ranging     : ADX < 20  — no clear direction; mean-reverting
    """
    # Spike detection: compare recent bar ranges to current ATR
    if len(candles) >= 20:
        recent_ranges = []
        for c in candles[-20:-1]:
            h  = float(c.get("high", 0) or 0)
            lo = float(c.get("low",  0) or 0)
            if h and lo:
                recent_ranges.append(h - lo)
        if recent_ranges:
            avg_range = sum(recent_ranges) / len(recent_ranges)
            if avg_range > 0 and atr > avg_range * 1.8:
                return "spike"

    adx = compute_adx(candles)
    if adx > 25:
        return "trending"
    if adx >= 20:
        return "weak_trend"
    return "ranging"


def compute_momentum_score(candles: list[dict], direction: str) -> float:
    """
    0.0 = no momentum, 1.0 = strong.
    Blends directional fraction and body/range ratio over last 12 candles.
    """
    if not candles:
        return 0.5

    window    = candles[-12:] if len(candles) >= 12 else candles
    direction = direction.upper()

    aligned_count = 0
    body_sum      = 0.0
    range_sum     = 0.0

    for c in window:
        o  = float(c.get("open",  0) or 0)
        cl = float(c.get("close", 0) or 0)
        h  = float(c.get("high",  0) or 0)
        lo = float(c.get("low",   0) or 0)
        if not (o and cl and h and lo):
            continue
        rng = h - lo
        if rng <= 0:
            continue
        body_sum  += abs(cl - o)
        range_sum += rng
        if (direction == "BUY"  and cl > o) or (direction == "SELL" and cl < o):
            aligned_count += 1

    n          = len(window)
    dir_score  = aligned_count / n if n else 0.5
    body_ratio = body_sum / range_sum if range_sum else 0.5
    return round(min(max(dir_score * 0.6 + body_ratio * 0.4, 0.0), 1.0), 3)


def find_swing_sl(
    candles:       list[dict],
    direction:     str,
    entry_price:   float,
    current_price: float,
    buffer:        float = 2.0,
) -> Optional[float]:
    """
    Find the most recent structural SL anchor between entry and current price.
    BUY:  last swing low in range, placed `buffer` below it.
    SELL: last swing high in range, placed `buffer` above it.
    Only considers levels that would lock in profit.
    """
    if len(candles) < 3:
        return None

    direction = direction.upper()

    for i in range(len(candles) - 2, -1, -1):
        if direction == "BUY":
            lo_p = float(candles[i - 1].get("low", 0) or 0)
            lo_c = float(candles[i    ].get("low", 0) or 0)
            lo_n = float(candles[i + 1].get("low", 0) or 0) if i + 1 < len(candles) else lo_c
            if lo_c < lo_p and lo_c < lo_n:
                candidate = round(lo_c - buffer, 2)
                if entry_price < candidate < current_price:
                    return candidate
        else:
            hi_p = float(candles[i - 1].get("high", 0) or 0)
            hi_c = float(candles[i    ].get("high", 0) or 0)
            hi_n = float(candles[i + 1].get("high", 0) or 0) if i + 1 < len(candles) else hi_c
            if hi_c > hi_p and hi_c > hi_n:
                candidate = round(hi_c + buffer, 2)
                if entry_price > candidate > current_price:
                    return candidate

    return None


# ── Calibrated parameter lookup ───────────────────────────────────────────────

def get_calibrated_multipliers(
    session: str,
    momentum_label: str,
    calibrated: dict,
) -> Optional[tuple[float, float, float]]:
    """
    Look up calibrated (be_mult, trail_mult, tp1_pct) for the given context.

    `calibrated` is the dict passed in from engine.py, keyed as
    "{session}_{momentum_label}".  Falls back to session-only key,
    then to "all_all".  Returns None if no calibration is available.
    """
    for key in (
        f"{session}_{momentum_label}",
        f"all_{momentum_label}",
        f"{session}_all",
        "all_all",
    ):
        if key in calibrated:
            entry = calibrated[key]
            if entry.get("sample_size", 0) >= _MIN_CAL_SAMPLES:
                return (
                    float(entry["be_multiplier"]),
                    float(entry["trail_multiplier"]),
                    float(entry["tp1_partial_pct"]),
                )
    return None


# ── Parameter computation ─────────────────────────────────────────────────────

def compute_dxy_bias(dxy_candles: list[dict], direction: str) -> float:
    """
    Returns 0.75 when DXY is trending against the trade (headwind), 1.0 otherwise.

    Gold and DXY are inversely correlated: rising DXY = headwind for XAUUSD BUY,
    falling DXY = headwind for SELL.  When a headwind is detected DPM trades
    defensively — tighter trail and earlier BE.

    Falls back to 1.0 (neutral) if fewer than 5 candles are available.
    """
    if not dxy_candles or len(dxy_candles) < 5:
        return 1.0
    window      = dxy_candles[-10:] if len(dxy_candles) >= 10 else dxy_candles
    first_close = float(window[0].get("close", 0) or 0)
    last_close  = float(window[-1].get("close", 0) or 0)
    if not (first_close and last_close):
        return 1.0
    dxy_rising = last_close > first_close
    headwind   = (direction.upper() == "BUY" and dxy_rising) or \
                 (direction.upper() == "SELL" and not dxy_rising)
    return 0.75 if headwind else 1.0


def compute_adaptive_params(
    trade:       dict,
    tick,
    candles:     list[dict],
    calibrated:  Optional[dict] = None,
    dxy_candles: Optional[list[dict]] = None,
    post_tp1:    bool = False,
) -> dict:
    """
    Compute fully adaptive DPM parameters.

    Priority: calibrated lookup → regime overlay → hardcoded defaults.

    Returns trail_distance, be_trigger_usd, tp1_partial_pct, swing_sl,
    reasoning, atr, adx, session, momentum, regime, be_multiplier,
    trail_multiplier, used_calibrated, dxy_bias, post_tp1.
    """
    direction   = (trade.get("direction") or "BUY").upper()
    entry_price = float(trade.get("entry_price") or 0)
    current     = (tick.bid if direction == "BUY" else tick.ask) if tick else entry_price

    atr      = compute_atr(candles)
    adx      = compute_adx(candles)
    session  = detect_session()
    regime   = detect_regime(candles, atr)
    momentum = compute_momentum_score(candles, direction)
    s_mult   = _SESSION_MULT.get(session, 1.0)
    r_cfg    = _REGIME_CONFIG.get(regime, _REGIME_CONFIG["ranging"])

    mom_label = (
        "strong"   if momentum >= 0.70 else
        "moderate" if momentum >= 0.45 else
        "weak"
    )

    # ── Base multipliers (regime + session) ───────────────────────────────────
    base_be_mult    = 0.6 * r_cfg["be_mult"]    # 0.6× ATR — triggers BE faster, cuts exposure earlier
    base_trail_mult = 1.2 * r_cfg["trail_mult"] # fixed at 1.2× ATR before regime adj

    # ── Momentum factor widens trail when trend is strong ─────────────────────
    momentum_factor = 1.0 + (momentum * 0.5)  # 1.0–1.5

    # ── Calibrated override if available ─────────────────────────────────────
    used_calibrated = False
    if calibrated:
        cal = get_calibrated_multipliers(session, mom_label, calibrated)
        if cal:
            base_be_mult, base_trail_mult, tp1_partial_pct = cal
            used_calibrated = True

    # Only fall back to momentum-based defaults when calibration did not supply
    # a tp1_partial_pct value.
    if not used_calibrated:
        if momentum >= 0.70:
            tp1_partial_pct = 0.25
        elif momentum >= 0.45:
            tp1_partial_pct = 0.40
        else:
            tp1_partial_pct = 0.60

    # ── DXY correlation bias ──────────────────────────────────────────────────
    dxy_bias = compute_dxy_bias(dxy_candles or [], direction)

    # ── Final computed values ─────────────────────────────────────────────────
    trail_distance = round(atr * base_trail_mult * s_mult * momentum_factor * dxy_bias, 1)
    trail_distance = max(_MIN_TRAIL, min(trail_distance, _MAX_TRAIL))

    # Once profit is locked in — TP1 cleared, or SL already moved to
    # breakeven via the direct BE-trigger threshold — halve the trail so
    # less of it gets given back before the stop catches a reversal. The
    # caller passes post_tp1=True for either case (see its call site).
    if post_tp1:
        trail_distance = max(_MIN_TRAIL, round(trail_distance * 0.5, 1))

    be_trigger = round(atr * base_be_mult * s_mult * dxy_bias, 1)
    be_trigger = max(_MIN_BE, min(be_trigger, _MAX_BE))

    swing_sl = find_swing_sl(candles, direction, entry_price, current)

    # ── Reasoning string for UI / logs ────────────────────────────────────────
    cal_tag  = " [calibrated]" if used_calibrated else ""
    adx_tag  = f"ADX {adx:.0f}"
    dxy_tag  = f" | DXY headwind [{dxy_bias:.2f}x]" if dxy_bias < 1.0 else ""
    tp1_tag  = " | tightened trail (BE locked)" if post_tp1 else ""
    reasoning = (
        f"ATR ${atr:.1f} | {adx_tag} ({regime}) | "
        f"{session.title()} session | Momentum {mom_label} ({momentum:.0%}) | "
        f"Trail ${trail_distance:.1f} | BE ${be_trigger:.1f} | "
        f"TP1 close {tp1_partial_pct:.0%}{cal_tag}{dxy_tag}{tp1_tag}"
    )
    if swing_sl:
        reasoning += f" | Structural SL ${swing_sl:.1f}"

    return {
        "trail_distance":    trail_distance,
        "be_trigger_usd":    be_trigger,
        "tp1_partial_pct":   tp1_partial_pct,
        "swing_sl":          swing_sl,
        "reasoning":         reasoning,
        "atr":               atr,
        "adx":               adx,
        "session":           session,
        "regime":            regime,
        "momentum":          momentum,
        "momentum_label":    mom_label,
        "be_multiplier":     base_be_mult,
        "trail_multiplier":  base_trail_mult,
        "used_calibrated":   used_calibrated,
        "dxy_bias":          dxy_bias,
        "post_tp1":          post_tp1,
    }


# ── Self-calibration ──────────────────────────────────────────────────────────

def run_calibration(trades: list[dict]) -> list[dict]:
    """
    Given a list of completed dpm_trade_performance records, compute
    optimal be_multiplier and trail_multiplier for each
    (session, momentum_bucket) combination.

    Returns a list of calibration result dicts ready for the repo to write
    to dpm_calibration and store in app_config.

    Requires at least _MIN_CAL_SAMPLES per group.
    """
    import time as _time

    BE_CANDIDATES    = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
    TRAIL_CANDIDATES = [0.8, 1.0, 1.2, 1.5, 1.8, 2.2]

    # Group trades
    groups: dict[tuple, list[dict]] = {}
    for t in trades:
        key = (
            t.get("session_at_entry", "all") or "all",
            t.get("momentum_label", "moderate") or "moderate",
        )
        groups.setdefault(key, []).append(t)

    # Also add an "all" group covering everything
    if len(trades) >= _MIN_CAL_SAMPLES:
        groups[("all", "all")] = trades

    results: list[dict] = []
    now = _time.time()

    for (session, mom_bucket), group in groups.items():
        if len(group) < _MIN_CAL_SAMPLES:
            continue

        # ── be_multiplier optimisation ────────────────────────────────────────
        best_be_pf   = -1.0
        best_be_mult = 1.0

        for be_m in BE_CANDIDATES:
            sim_outcomes: list[float] = []
            for t in group:
                atr  = float(t.get("atr_at_entry") or 10.0)
                lot  = float(t.get("lot_size")      or 0.01)
                peak = float(t.get("peak_pnl")      or 0.0)
                pnl  = float(t.get("final_pnl")     or 0.0)
                orig_sl = float(t.get("original_sl") or 0.0)
                entry   = float(t.get("entry_price") or 0.0)
                sess    = t.get("session_at_entry", "london") or "london"
                sm      = _SESSION_MULT.get(sess, 1.0)

                # Units: atr ($/oz) * lot (lots) * 100 (oz/lot) = USD
                # sm is the session multiplier (0.80–1.40) that scales the BE
                # threshold per session, matching compute_adaptive_params behaviour.
                hypothetical_be = be_m * atr * sm * lot * 100.0
                sl_loss = abs(entry - orig_sl) * lot * 100.0 if orig_sl else abs(min(pnl, 0))

                if peak >= hypothetical_be:
                    # BE would have triggered
                    if pnl < -0.50:                       # actual SL hit → BE saves it
                        sim_outcomes.append(0.0)
                    else:
                        sim_outcomes.append(pnl)          # trail or TP exit, unchanged
                else:
                    # BE would NOT have triggered
                    if t.get("reached_be") and abs(pnl) < 0.50:  # actual BE exit → now SL
                        sim_outcomes.append(-sl_loss)
                    else:
                        sim_outcomes.append(pnl)

            wins   = sum(p for p in sim_outcomes if p > 0)
            losses = abs(sum(p for p in sim_outcomes if p < 0))
            pf     = wins / max(losses, 0.01)

            if pf > best_be_pf:
                best_be_pf   = pf
                best_be_mult = be_m

        # ── trail_multiplier optimisation ─────────────────────────────────────
        # Use capture ratio for trailing exits: final_pnl / peak_pnl.
        # A low ratio means the trail is too wide (gave back too much profit).
        # We scale the recommended multiplier by the observed capture efficiency.
        trail_exits = [t for t in group if (t.get("exit_type") or "") == "trail"]
        best_trail_mult = 1.2  # default
        if len(trail_exits) >= 3:
            capture_ratios = []
            for t in trail_exits:
                peak = float(t.get("peak_pnl") or 0.0)
                pnl  = float(t.get("final_pnl") or 0.0)
                if peak > 0:
                    capture_ratios.append(pnl / peak)
            if capture_ratios:
                avg_capture = sum(capture_ratios) / len(capture_ratios)
                # avg_capture < 0.40 → trail too wide → tighten → lower multiplier
                # avg_capture > 0.70 → trail working well
                if avg_capture < 0.30:
                    best_trail_mult = 0.9
                elif avg_capture < 0.50:
                    best_trail_mult = 1.0
                elif avg_capture > 0.70:
                    best_trail_mult = 1.3
                else:
                    best_trail_mult = 1.2

        # ── tp1_partial_pct: keep momentum-based defaults ─────────────────────
        # Calibrate only if we have clear signal in the data.
        tp1_pct = 0.40  # moderate default
        if mom_bucket == "strong":
            tp1_pct = 0.25
        elif mom_bucket == "weak":
            tp1_pct = 0.60

        # ── Win rate and avg R for reporting ─────────────────────────────────
        rs       = [float(t.get("r_multiple") or 0.0) for t in group]
        win_rate = sum(1 for r in rs if r > 0) / len(rs) if rs else 0.0
        avg_r    = sum(rs) / len(rs) if rs else 0.0

        results.append({
            "calibrated_at":  now,
            "session":        session,
            "momentum_bucket": mom_bucket,
            "be_multiplier":  best_be_mult,
            "trail_multiplier": best_trail_mult,
            "tp1_partial_pct": tp1_pct,
            "sample_size":    len(group),
            "profit_factor":  round(best_be_pf, 3),
            "win_rate":       round(win_rate, 3),
            "avg_r_multiple": round(avg_r, 3),
            "notes": (
                f"BE mult {best_be_mult:.2f} (PF {best_be_pf:.2f}) | "
                f"Trail mult {best_trail_mult:.2f} | "
                f"n={len(group)}"
            ),
        })

    return results
