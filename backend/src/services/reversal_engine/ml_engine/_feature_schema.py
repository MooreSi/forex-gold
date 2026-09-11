"""The feature vector's shape: its names, and the neutral for each.

Split out of `ml_engine/__init__.py` on 2026-09-11 — it was 785 lines against
a hard 800 and `reversal-engine/070` (the fleet model) edits `predict()` in it.
Verbatim: the list, the neutrals and their reasoning are unchanged.

It lives in its own module rather than beside `extract_features` because
`_training_data.py` needs both names to right-pad a historical row, and
`extract_features` stays in `__init__` where the mutable model state is. Two
importers, no state: that is what makes this a safe seam and the rest of the
file not one.

**Append only.** Padding a short old vector on the right is correct only while
every existing position still means what it meant when that row was written.
`ml_handover` depends on the same property.
"""
from __future__ import annotations

from backend.src.services.reversal_engine.re_macro import (
    MACRO_FEATURE_NAMES, MACRO_NEUTRAL)

_LEVEL_TYPES = ["asia_low", "asia_high", "swing_high", "swing_low",
                "round_10", "round_5", "congestion"]

FEATURE_NAMES = [
    "level_score",          # 0-1
    "level_type_asia",      # 1 if asia_low or asia_high
    "level_type_swing",     # 1 if swing
    "level_type_round",     # 1 if round number
    "htf_bias_score",       # bullish=+1, neutral=0, bearish=-1
    "direction_score",      # BUY=+1, SELL=-1
    "bias_aligned",         # 1 if bias aligns with direction
    "session_score",        # overlap=1.5, london=1.0, ny=0.8, asian=0.5, off=0
    "adx_norm",             # adx/50 clamped [0,1]
    "atr_norm",             # atr/20 clamped [0,1]
    "hour_sin",             # sin(hour * 2π/24)
    "hour_cos",             # cos(hour * 2π/24)
    "distance_norm",        # level distance / atr, clamped [0,5]
    "recent_win_rate",      # last 20 closed signals
    "ref_level_win_rate",   # historical REF win rate for this level type
    "rr_tp1",               # risk:reward to TP1 clamped [0,5]
    "minutes_since_ref_norm",   # minutes since the last real REF signal / 240, clamped [0,1]
    "ref_signals_today_norm",   # real REF signals received so far today / 10, clamped [0,1]
    "news_proximity_norm",      # minutes to next high-impact event / 120, clamped [0,1]; 0=imminent, 1=safe
    "regime_score",             # trending=1.0, ranging=0.0, volatile=0.5 (derived from ADX+ATR)
    "equity_drawdown_pct",      # current drawdown from peak equity [0,1]
    "concurrent_agreement",     # +1 same-dir signal from another engine in last 15min, -1 opposite, 0 none
    "ref_discipline_score",     # 0-1, AI-derived nightly: how closely the reference channel/GD2 stuck to stated SL/sizing that day
    "ref_aggression_score",     # 0-1, AI-derived nightly: how aggressively they scaled in/chased entries that day
    # ── FVG context (v6, 2026-08-04) — see ict_patterns.fvg_context ──────
    "fvg_confluence",           # 1.0 entry inside an aligned unfilled FVG, 0.5 inside any, 0 none
    "fvg_dist_norm",            # distance to nearest aligned FVG in ATR units, clamped [0,5]; 5 = none
    "fvg_fresh",                # nearest aligned FVG: 1.0 untested, 0.5 filled, 0.0 inverted; 0.5 = none
    "fvg_size_norm",            # its height in ATR units, clamped [0,3]; 0 = none
    # ── Reference-channel entry structure (v7, 2026-08-05) ───────────────
    # How this moment compares with conditions the professional channels
    # actually fire in, per direction. See reversal_engine/pro_profile.py --
    # these stay at 0.0 until that module judges its sample trustworthy, so
    # early rows carry no information rather than a regime artifact.
    "pro_rsi_delta",            # (rsi - their median) / their sd, clamped [-3,3]
    "pro_adx_delta",            # same for ADX
    "pro_fvg_delta",            # our FVG confluence minus theirs
    "pro_profile_ready",        # 1.0 when the profile is live, else 0.0
    # ── Pro-likeness (v8, 2026-08-06) — see reversal_engine/pro_model.py ──
    # P(a reference channel would fire in this moment), from a classifier
    # trained on their captured entries against background samples. 0.5 --
    # indistinguishable from "exactly average" -- whenever the learning
    # toggle is off, the corpus gates are unmet, or the model cannot beat a
    # coin out of sample. All three mean the same thing to a model: no
    # information, so they must produce the same number.
    "pro_likeness",
    # ── Macro context (v9, 2026-09-05) — see reversal_engine/re_macro.py ──
    *MACRO_FEATURE_NAMES,
]

# Neutral value for every feature, used to back-fill rows labeled under an
# earlier _version so a feature addition does not throw away training
# history. Keyed by name rather than position so it cannot silently drift.
#
# THIS IS WHY NEW FEATURES MUST ONLY EVER BE APPENDED to FEATURE_NAMES,
# never inserted mid-list: padding a short old vector on the right is only
# correct if the existing positions still mean what they meant when that
# row was written.
_FEATURE_NEUTRAL = {
    "fvg_confluence": 0.0, "fvg_dist_norm": 5.0,
    "fvg_fresh": 0.5, "fvg_size_norm": 0.0,
    "pro_rsi_delta": 0.0, "pro_adx_delta": 0.0,
    "pro_fvg_delta": 0.0, "pro_profile_ready": 0.0,
    "pro_likeness": 0.5,
    **MACRO_NEUTRAL,
}


def pad_to_schema(vector):
    """Right-pad a short vector with each missing feature's neutral, or
    None when it cannot be interpreted.

    Lifted verbatim out of `_training_data._get_training_data` on
    2026-09-11 so `macro_backfill` repairs a stored row exactly the way
    training pads one in memory. Two copies of this would be two
    definitions of what an old row means.

    A vector LONGER than the current schema is from a newer build and is
    still uninterpretable, so it returns None rather than being truncated.
    """
    f = list(vector)
    if len(f) > len(FEATURE_NAMES):
        return None
    if len(f) < len(FEATURE_NAMES):
        f = f + [_FEATURE_NEUTRAL.get(n, 0.0)
                 for n in FEATURE_NAMES[len(f):]]
    return f
