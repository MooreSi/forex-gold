"""
Adaptive parameters for the BREAKOUT signal engine.
Stored separately in bo_config — no cross-contamination with bounce engine params.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

_log = logging.getLogger("breakout_signal")

PARAMS: dict[str, dict] = {
    "min_adx_go": {
        # 2026-07-16 rebuild: the earlier 40→45 ratchet (based on a small
        # positive 50+ bucket) forced EVERY entry into mature/extended trends —
        # and the 40+ bucket itself lost -$1,258 over 191 trades. Standard ADX
        # practice: a RISING ADX from a moderate base signals a fresh trend leg
        # (what a breakout needs); a high absolute ADX means the move is late.
        # Floor lowered back to moderate; lateness now handled by
        # max_adx_entry + require_adx_rising instead of an ever-higher floor.
        "default": 28.0, "min": 20.0, "max": 45.0,
        "desc": "Minimum ADX for break-and-go signals (trend igniting, not exhausted)",
    },
    "min_adx_retest": {
        # 2026-07-16 rebuild: see min_adx_go. Retests happen on the pullback
        # where ADX naturally dips — a high floor here rejected exactly the
        # healthy pullbacks and accepted only late-trend chop.
        "default": 24.0, "min": 18.0, "max": 40.0,
        "desc": "Minimum ADX for break-and-retest signals (lower threshold since break already validated trend)",
    },
    "max_adx_entry": {
        # Exhaustion cap (2026-07-16): 66% of all closed trades fired at ADX
        # 40+ and that bucket lost -$1,258; HTF-aligned entries lost while
        # neutral-bias (earlier-trend) entries won 57.7% — both symptoms of
        # buying tops of extended legs. Above this ADX the trend is mature:
        # no NEW entries (matches the 'peak and hook above 40-50 = exhaustion'
        # consensus in the ADX literature).
        "default": 48.0, "min": 35.0, "max": 60.0,
        "desc": "Maximum ADX for any new entry — above this the trend is extended/exhausted",
    },
    "require_adx_rising": {
        "default": 1.0, "min": 0.0, "max": 1.0,
        "desc": "Require ADX to be rising vs ~3 candles ago for break-and-go entries "
                "(rising ADX = fresh trend leg; falling ADX at a break = fade risk)",
    },
    "min_break_pts": {
        "default": 3.0, "min": 1.0, "max": 10.0,
        "desc": "Minimum points the candle BODY must close beyond the level to count as a valid break",
    },
    "retest_atr_tolerance": {
        "default": 0.40, "min": 0.15, "max": 0.70,
        "desc": "How close to the broken level price must be to qualify as a retest (ATR × this value)",
    },
    "lookback_candles": {
        "default": 8.0, "min": 3.0, "max": 15.0,
        "desc": "How many M15 candles back to search for a prior break when detecting retest entries",
    },
    "sl_atr_mult": {
        # 2026-07-16 rebuild: 0.35×ATR buffers produced an average SL distance
        # of 3.76pts = 0.61×ATR — inside ordinary M5 noise. 123 of 144 losses
        # (85%) stopped out within 15 minutes: noise stops, not wrong calls.
        # Consensus (and a 50-failed-breakout stop study): 1-1.5×ATR beyond
        # the level. Stops at the obvious local extreme get hunted.
        "default": 1.0, "min": 0.50, "max": 1.80,
        "desc": "Extra ATR buffer added beyond the broken level for the stop-loss",
    },
    "min_sl_dist_atr": {
        "default": 1.0, "min": 0.60, "max": 2.00,
        "desc": "Floor on total SL distance from entry (× ATR) — a stop closer than "
                "this sits inside M5 noise regardless of where the level is",
    },
    "max_sl_dist_atr": {
        "default": 2.5, "min": 1.50, "max": 4.00,
        "desc": "Cap on total SL distance from entry (× ATR) — wider means the entry "
                "is chasing too far from the broken level; skip instead",
    },
    "compression_max_range_atr": {
        # The single most documented breakout-quality predictor: volatility
        # contraction BEFORE the break (squeeze → expansion). A break out of a
        # tight consolidation has fuel behind it; a 'break' that is just
        # another candle in an already-running move is continuation-chasing —
        # which is what this engine was doing (fvg_bull mid-move breaks:
        # 35.6% WR, -$812).
        "default": 2.5, "min": 1.50, "max": 5.00,
        "desc": "Break-and-go requires the 12 candles before the breakout candle to "
                "span no more than this × ATR (tight consolidation = real breakout)",
    },
    "retest_max_prior_touches": {
        "default": 1.0, "min": 1.0, "max": 3.0,
        "desc": "Maximum times the level may already have been retested since the "
                "break — first pullback has the edge, later touches erode the level",
    },
    "min_quality_score": {
        "default": 0.45, "min": 0.30, "max": 0.85,
        "desc": "Minimum Claude quality score (0–1) required to take a breakout signal",
    },
    "require_dual_bias": {
        "default": 1.0, "min": 0.0, "max": 1.0,
        "desc": "Require BOTH H1 and H4 bias to agree with direction (1=strict, 0=H1 only)",
    },
    "min_break_atr_frac": {
        "default": 0.12, "min": 0.05, "max": 0.30,
        "desc": "Minimum break distance as a fraction of ATR (volatility-scaled floor on top of min_break_pts)",
    },
    "exhaustion_atr_mult": {
        "default": 1.4, "min": 1.0, "max": 2.5,
        "desc": "Reject break-and-go if the breakout candle body exceeds this many ATRs (climactic/exhaustion spike, prone to revert)",
    },
    "min_level_strength": {
        "default": 2.0, "min": 1.0, "max": 4.0,
        "desc": "Minimum key-level strength required to qualify as a tradeable breakout level (filters noisy round-number levels)",
    },
    "tp1_mult": {
        "default": 1.0, "min": 0.6, "max": 2.0,
        "desc": "TP1 distance as a multiple of SL distance (partial close booked here)",
    },
    "tp2_mult": {
        # 2026-07-16: widened 1.8→2.0. Realized avg win ($37) was SMALLER than
        # avg loss ($46) — fatal for a breakout system, whose entire edge is
        # asymmetry (documented squeeze-breakout systems win 35-55% but pay
        # 2R+ per win). Thirds at 1R/2R/3.5R give a full winner ≈ 2.17R vs a
        # 1R full loss → break-even win rate ≈ 31.5%.
        "default": 2.0, "min": 1.2, "max": 3.5,
        "desc": "TP2 distance as a multiple of SL distance (second partial close booked here)",
    },
    "tp3_mult": {
        "default": 3.5, "min": 1.8, "max": 6.0,
        "desc": "TP3 distance as a multiple of SL distance (runner close — final exit for remainder)",
    },
    "sweep_wick_atr_frac": {
        "default": 0.15, "min": 0.05, "max": 0.50,
        "desc": "Minimum wick pierce beyond the level (ATR fraction) to qualify as a liquidity sweep",
    },
    "max_adx_sweep": {
        "default": 45.0, "min": 25.0, "max": 60.0,
        "desc": "Maximum ADX for sweep-reversal entries (strong trends run through swept levels instead of reversing)",
    },
    "sweep_sl_atr_mult": {
        "default": 0.35, "min": 0.10, "max": 0.80,
        "desc": "ATR buffer beyond the sweep wick extreme for the stop-loss",
    },
    "max_spread_pts": {
        "default": 0.50, "min": 0.30, "max": 1.50,
        "desc": "Maximum bid-ask spread (price units) to allow new signals — widened spread doubles scalp entry cost",
    },
    # NOTE: "hour_filter_enabled" removed 2026-07-16 — it was dead code (defined
    # here but never read anywhere; the real toxic-hour gate is the
    # hour_blocklist_enabled risk setting checked in _execute_live). The nightly
    # AI tuner had set it to 0 believing it was disabling a filter.
    "level_filter_enabled": {
        "default": 1.0, "min": 0.0, "max": 1.0, "tuner_locked": True,
        "desc": "Block breakouts through fvg_bull (-$793, 37% WR) and round (-$223, 36% WR) "
                "levels. 0 = allow all level types",
    },
    "time_stop_mins": {
        # 2026-07-16: 15→30. Winners averaged 14.9min to resolve vs 9.2min for
        # losses — a 15-minute guillotine on anything not yet in profit was
        # cutting exactly the slow winners (37 wins took >15min). The engine
        # also now only time-stops trades that are demonstrably FAILING
        # (see _check_outcomes), not ones hovering around flat.
        "default": 30.0, "min": 15.0, "max": 60.0,
        "desc": "Close a triggered trade at market if it is in a clear loss after this many "
                "minutes with no partial banked — a failing breakout rarely recovers",
    },
    "daily_loss_stop_usd": {
        "default": 200.0, "min": 100.0, "max": 500.0, "tuner_locked": True,
        "desc": "Stop generating new signals for the rest of the UTC day once today's closed PnL "
                "reaches this loss (Jul 2 ran to -$457 unchecked)",
    },
}

_DB_PREFIX = "bo_ap:"


def _tdb():
    from forex_trader.breakout_signal import breakout_signal_repo as _db
    return _db


def get(key: str) -> float:
    if key not in PARAMS:
        raise KeyError(f"Unknown breakout adaptive param: {key!r}")
    raw = _tdb().get_config(_DB_PREFIX + key, "")
    if raw:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return float(PARAMS[key]["default"])


def get_all() -> dict[str, dict]:
    result = {}
    for k, meta in PARAMS.items():
        result[k] = {
            "value":   get(k),
            "default": meta["default"],
            "min":     meta["min"],
            "max":     meta["max"],
            "desc":    meta["desc"],
        }
    return result


def apply_adjustment(key: str, raw_value: float, reason: str = "") -> Optional[float]:
    import time as _t
    if key not in PARAMS:
        _log.warning("[BO-AP] Unknown param %r — skipped", key)
        return None
    meta    = PARAMS[key]
    # Structural safety params stay out of the AI tuner's reach: the nightly
    # batch analysis previously disabled hour filtering and ratcheted ADX
    # floors into the losing bucket. Locked params change only via explicit
    # code/UI decisions, never via automated tuning.
    if meta.get("tuner_locked"):
        _log.info("[BO-AP] %s is tuner-locked — adjustment to %.4g refused (%s)",
                  key, raw_value, reason)
        return None
    clamped = round(min(meta["max"], max(meta["min"], float(raw_value))), 4)
    old_val = get(key)
    if abs(clamped - old_val) < 1e-6:
        return None
    _tdb().set_config(_DB_PREFIX + key, str(clamped))
    _log.info("[BO-AP] %s: %.4f → %.4f  reason=%r", key, old_val, clamped, reason)
    _tdb().log_analysis({
        "ts":             _t.time(),
        "result":         f"param_change:{key}",
        "claude_decision": f"{key}: {old_val:.4g} → {clamped:.4g}  |  {reason}",
    })
    return clamped


def reset_to_defaults() -> None:
    for key in PARAMS:
        _tdb().set_config(_DB_PREFIX + key, str(PARAMS[key]["default"]))
    _log.info("[BO-AP] All parameters reset to defaults")


def catalogue_for_prompt() -> str:
    lines = []
    for k, info in get_all().items():
        lines.append(
            f"  {k}: current={info['value']:.4g}  "
            f"range=[{info['min']}, {info['max']}]  — {info['desc']}"
        )
    return "\n".join(lines)
