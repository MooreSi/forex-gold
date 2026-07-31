"""
Adaptive parameter store for the TEST signal engine.

Parameters are seeded with safe defaults on first run, then updated by Claude's
batch analysis after every 10 closed trades.  All changes are persisted in the
test_signal DB (test_config table) and logged to the analysis log so the full
learning history is visible in the UI.

Safety: every Claude-recommended value is clamped to the defined [min, max] range
before being applied.  The engine never operates outside the safe envelope.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

_log = logging.getLogger("test_signal")

# ── Parameter catalogue ───────────────────────────────────────────────────────
# Each entry: default value, safe min, safe max, human description.

PARAMS: dict[str, dict] = {
    "min_quality_score": {
        "default": 0.40, "min": 0.25, "max": 0.85,
        "desc": "Minimum Claude quality score (0–1) required to take a signal",
    },
    "zone_width_atr_mult": {
        "default": 0.75, "min": 0.25, "max": 1.20,
        "desc": "Entry zone width = ATR × this multiplier",
    },
    "sl_atr_mult": {
        "default": 0.80, "min": 0.50, "max": 1.50,
        "desc": "Stop loss buffer = ATR × this multiplier beyond the key level",
    },
    "min_rr": {
        # Raised from 1.00 (2026-07-06): realized payoff ran 0.65:1 (avg win
        # $37.57 vs avg loss $57.48, n=279) — at 57% WR the engine needed 60%
        # just to break even. TP1 must clear more than the stop risks.
        "default": 1.25, "min": 0.80, "max": 2.00,
        "desc": "Minimum risk:reward ratio to TP1 — signals below this are discarded",
    },
    "allow_asian": {
        "default": 1.0, "min": 0.0, "max": 1.0,
        "desc": "Enable signals during Asian session (0 = off, 1 = on) — Asian signals require trend alignment",
    },
    "allow_counter_bias": {
        "default": 0.0, "min": 0.0, "max": 1.0,
        "desc": "Allow signals that go against the H1 HTF bias (0 = strict, 1 = allow)",
    },
    "dual_bias_adx_block": {
        "default": 30.0, "min": 20.0, "max": 50.0,
        "desc": "ADX threshold above which counter-trend signals are blocked when H1+H4 bias both agree",
    },
    "max_spread_pts": {
        "default": 0.50, "min": 0.30, "max": 1.50,
        "desc": "Maximum bid-ask spread (price units) to allow new signals — widened spread doubles scalp entry cost",
    },
    "extreme_trend_adx": {
        "default": 35.0, "min": 28.0, "max": 50.0,
        "desc": "ADX threshold above which mean-reversion patterns (bounce, liquidity_sweep) are "
                "blocked regardless of trade direction — structural levels stop holding once a "
                "trend is this persistent, so fading them (even with-trend) stops paying off",
    },
    "time_stop_mins": {
        "default": 25.0, "min": 10.0, "max": 60.0,
        "desc": "Close a triggered trade at market if it is not meaningfully in profit after this "
                "many minutes — losses averaged 26min to full-SL while winners were positive well "
                "before; cutting stalled trades early caps the loss side of the payoff ratio",
    },
    "early_be_frac": {
        "default": 0.50, "min": 0.30, "max": 0.90,
        "desc": "Move SL to breakeven+cost once unrealized profit reaches this fraction of the SL "
                "distance — the old TP1-only BE trigger never engaged on a single one of 117 losses",
    },
    "daily_loss_stop_usd": {
        "default": 200.0, "min": 100.0, "max": 500.0,
        "desc": "Stop generating new signals for the rest of the UTC day once today's closed PnL "
                "reaches this loss — prevents one hostile day from compounding (Jul 6: -$586)",
    },
    "hour_filter_enabled": {
        "default": 1.0, "min": 0.0, "max": 1.0,
        "desc": "Enable the measured toxic-hour block (UTC 07,12-16,20 — combined -$1,377 across "
                "all history). 0 = trade all hours",
    },
}

# Params learned separately per market regime instead of one global value — these
# are the risk-management / gate knobs that should behave differently on a trending
# day vs a ranging day. Everything else in PARAMS stays a single global value.
REGIME_SPLIT_KEYS = {"min_quality_score", "sl_atr_mult", "min_rr", "early_be_frac", "time_stop_mins"}
# 5-state day taxonomy from core/regime.py (classify_day). The engine stores
# these on each signal's `regime` column, so batch analysis groups by them and
# per-regime overrides key off them directly.
REGIMES = ("strong_trend", "trend", "range", "volatile_chop", "neutral")

_DB_PREFIX = "ap:"   # key prefix used in test_config table


def _tdb():
    from backend.src.services.test_signal import test_signal_repo as _db
    return _db


def get(key: str, regime: Optional[str] = None) -> float:
    """
    Return current value for a parameter, falling back to default.

    If `regime` is given and `key` is in REGIME_SPLIT_KEYS, a regime-specific
    learned value is tried first (falls back to the global value, then the
    hardcoded default, if no regime-specific value has been learned yet).
    """
    if key not in PARAMS:
        raise KeyError(f"Unknown adaptive param: {key!r}")
    if regime and key in REGIME_SPLIT_KEYS and regime in REGIMES:
        raw = _tdb().get_config(f"{_DB_PREFIX}{regime}:{key}", "")
        if raw:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    raw = _tdb().get_config(_DB_PREFIX + key, "")
    if raw:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return float(PARAMS[key]["default"])


def get_all() -> dict[str, dict]:
    """Return {param_name: {value, default, min, max, desc}} for all params."""
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


def apply_adjustment(
    key: str, raw_value: float, reason: str = "", regime: Optional[str] = None
) -> Optional[float]:
    """
    Clamp raw_value to the safe range and persist.

    If `regime` is given and `key` is in REGIME_SPLIT_KEYS, the value is stored
    as a regime-specific override rather than overwriting the global default —
    e.g. a bad trending day tightens `sl_atr_mult` only for future trending-regime
    signals, leaving ranging-day behaviour untouched.

    Returns the clamped value that was applied, or None if unchanged.
    """
    if key not in PARAMS:
        _log.warning("[AP] Unknown param %r — skipped", key)
        return None

    use_regime = regime if (regime in REGIMES and key in REGIME_SPLIT_KEYS) else None

    meta = PARAMS[key]
    clamped = round(min(meta["max"], max(meta["min"], float(raw_value))), 4)
    old_val = get(key, regime=use_regime)

    if abs(clamped - old_val) < 1e-6:
        return None   # no change

    db_key = f"{_DB_PREFIX}{use_regime}:{key}" if use_regime else (_DB_PREFIX + key)
    _tdb().set_config(db_key, str(clamped))
    tag = f" [{use_regime}]" if use_regime else ""
    _log.info(
        "[AP] %s%s: %.4f → %.4f  reason=%r", key, tag, old_val, clamped, reason
    )
    _tdb().log_analysis({
        "ts":     time.time(),
        "result": f"param_change:{key}" + (f":{use_regime}" if use_regime else ""),
        "claude_decision": (
            f"{key}{tag}: {old_val:.4g} → {clamped:.4g}  |  {reason}"
        ),
    })
    return clamped


def reset_to_defaults() -> None:
    """Wipe all learned values — resets to out-of-box defaults."""
    for key in PARAMS:
        _tdb().set_config(_DB_PREFIX + key, str(PARAMS[key]["default"]))
    _tdb().log_analysis({
        "ts":     time.time(),
        "result": "param_reset",
        "claude_decision": "All adaptive parameters reset to factory defaults.",
    })
    _log.info("[AP] All parameters reset to defaults")


def catalogue_for_prompt() -> str:
    """Format the full parameter catalogue for inclusion in a Claude prompt."""
    lines = []
    all_p = get_all()
    for k, info in all_p.items():
        split_note = "  (learnable per-regime: trending/ranging/neutral)" if k in REGIME_SPLIT_KEYS else ""
        lines.append(
            f"  {k}: current={info['value']:.4g}  "
            f"range=[{info['min']}, {info['max']}]  — {info['desc']}{split_note}"
        )
    return "\n".join(lines)


def get_regime_overrides() -> dict[str, dict[str, float]]:
    """Return only the REGIME_SPLIT_KEYS overrides that have actually been
    learned (a row exists in test_config for that regime), keyed
    {param: {regime: value}} — for the UI's Learned Parameters panel.

    get_all()/get() only surface each key's *global* fallback value, so a
    param whose only learning so far is regime-specific (the common case —
    Claude's batch analysis almost always tags its adjustments with a regime,
    see engine.py's _run_batch_analysis) shows as "unchanged from default"
    there even though real learning happened. Confirmed live 2026-07-13:
    4 of 5 applied adjustments in test_config were regime-scoped and
    invisible in that table."""
    out: dict[str, dict[str, float]] = {}
    for key in REGIME_SPLIT_KEYS:
        for regime in REGIMES:
            raw = _tdb().get_config(f"{_DB_PREFIX}{regime}:{key}", "")
            if raw:
                try:
                    out.setdefault(key, {})[regime] = float(raw)
                except (ValueError, TypeError):
                    pass
    return out


def regime_catalogue_for_prompt() -> str:
    """
    Show current per-regime learned overrides for REGIME_SPLIT_KEYS, so Claude's
    batch analysis can see what's already been learned for each regime before
    proposing further adjustments.
    """
    lines = []
    for key in sorted(REGIME_SPLIT_KEYS):
        parts = [f"global={get(key):.4g}"]
        for regime in REGIMES:
            parts.append(f"{regime}={get(key, regime=regime):.4g}")
        lines.append(f"  {key}: {'  '.join(parts)}")
    return "\n".join(lines)
