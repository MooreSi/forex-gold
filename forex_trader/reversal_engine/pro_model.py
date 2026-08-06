"""Pro-likeness: one model, one number -- "does this moment look like one the
professionals would fire in?" (2026-08-06)

WHAT IT IS
----------
A classifier over the corpus in pro_corpus.py:

    positive  a moment Gold Diggers VIP / INSTITUTIONAL actually fired
    negative  a background sample, same market, nobody fired

Its output probability becomes ONE feature (`pro_likeness`) in the Reversal
Engine's own model. That containment is the design, not an implementation
detail: the main model regresses realised R on OUR signals, and a reference
signal has neither a comparable feature vector nor an R of ours. Pooling the
two would answer a different question with the same weights. As a single
input, LightGBM weighs this opinion against its own evidence and can ignore
it outright if it turns out to be noise.

WHY IT SUPERSEDES pro_profile's HAND-MADE DELTAS
------------------------------------------------
pro_profile emits signed distances from their median RSI/ADX/FVG. That is a
linear, per-dimension approximation of this question, and it cannot express
"high RSI is fine for them in London but not in Asia". Those features stay
(the feature list is append-only, and old rows were labelled with them), but
this is the one meant to carry the signal.

WEIGHTING BY OUTCOME
--------------------
pro_outcome.py resolves each captured signal against its own stated levels.
Winners are weighted up and losers down (_OUTCOME_WEIGHT) rather than losers
being dropped: the target here is where a professional CHOOSES to act, and a
disciplined entry that lost is still evidence of that judgement -- just
weaker evidence than one that paid. Unresolved rows keep weight 1.0, so the
model is never blocked waiting on outcomes.

THREE THINGS IT REFUSES TO DO
-----------------------------
1. Speak before the corpus is honest. Same gates as pro_profile (60/60 and a
   real RSI spread), for the same reason: the first sample there came from a
   single sustained rally and encoded the week, not their logic.
2. Speak when it cannot beat a coin. Held-out AUC below _MIN_AUC returns the
   neutral 0.5 -- the same value as "no model" -- so a model that has learned
   nothing contributes nothing instead of contributing noise.
3. Learn from a feature the live path cannot supply. Every input below is
   one the Reversal Engine already has at scoring time; anything richer in
   the snapshot (M1/M5 blocks, volume, EMA stacking) is deliberately left out
   because a feature present in training and missing in production is worse
   than no feature at all.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from forex_trader.reversal_engine import pro_corpus
from forex_trader.reversal_engine.pro_profile import (
    _MIN_NEGATIVES, _MIN_POSITIVES, _MIN_RSI_SPREAD,
)

log = logging.getLogger(__name__)

NEUTRAL = 0.5

# Held-out ROC AUC the model must clear before its opinion is used at all.
_MIN_AUC = 0.55
# Weight applied to a positive by how its own call resolved (pro_outcome.py).
_OUTCOME_WEIGHT = {"win": 1.5, "loss": 0.6, "timeout": 0.9, "no_fill": 0.5}

# NO CLOCK FEATURES HERE, DELIBERATELY
# -------------------------------------
# An earlier cut included session_score/hour_sin/hour_cos and scored 0.868
# held-out AUC on the real corpus (123 positives, 210 background, 2026-08-06).
# Measured by ablation, the clock alone reached 0.728 of that: background
# samples are taken every 15 minutes around the clock, including the hours
# these channels never post, so "is it a session they trade" was doing a
# third of the work. That is real information, but the Reversal Engine's own
# feature vector ALREADY carries hour_sin/hour_cos/session_score -- letting
# this model re-encode the hour would hand the same fact to LightGBM twice
# under two names. Stripped of the clock the model reads 0.834.
#
# NO DIRECTION FEATURE EITHER, FOR A SHARPER REASON
# -------------------------------------------------
# capture_background_snapshot writes exactly one BUY and one SELL row per
# tick, so the negatives are 50/50 by construction. The positives are
# whatever the channels actually posted -- 105 BUY to 18 SELL in the first
# corpus. A direction input therefore separates the classes without carrying
# any information about their logic: "is it a BUY" alone scored 0.669 AUC,
# and a BUY in dead conditions came out at 0.997 pro-like. That is the
# sampling scheme leaking into the label. Direction still reaches the model
# where it belongs -- every FVG feature below is computed relative to the
# trade's own direction, and the Reversal Engine's vector has direction_score
# of its own.
#
# What survives both removals is 0.763 held-out AUC on market structure
# alone -- lower than the headline 0.868, and the only one of the three
# numbers that means what it appears to mean.
FEATURE_NAMES = [
    "rsi_norm",         # M15 RSI / 100
    "adx_norm",         # M15 ADX / 50, clamped [0,1]
    "atr_norm",         # M15 ATR / 20, clamped [0,1]
    "regime_score",     # trending=1.0 ... ranging=0.0
    "fvg_confluence",
    "fvg_dist_norm",
    "fvg_fresh",
    "fvg_size_norm",
]

_state: dict = {"model": None, "scaler": None, "auc": None, "n": 0,
                "fitted_at": 0.0, "rows_at_fit": -1, "reason": "not fitted"}


# ── Feature construction ──────────────────────────────────────────────────────

def _vector(direction: str, rsi: Optional[float], adx: Optional[float],
            atr: Optional[float], regime: Optional[float],
            fvg: dict) -> Optional[list[float]]:
    if rsi is None or adx is None:
        return None
    # `direction` is still taken (and still required) because the FVG block
    # handed in below is direction-relative -- it is simply not a feature of
    # its own. See FEATURE_NAMES.
    return [
        float(rsi) / 100.0,
        min(float(adx) / 50.0, 1.0),
        min(float(atr or 8.0) / 20.0, 1.0),
        float(regime if regime is not None else 0.5),
        float(fvg.get("fvg_confluence") or 0.0),
        float(fvg.get("fvg_dist_norm", 5.0) or 5.0),
        float(fvg.get("fvg_fresh", 0.5) if fvg.get("fvg_fresh") is not None else 0.5),
        float(fvg.get("fvg_size_norm") or 0.0),
    ]


def _row_vector(row: dict) -> Optional[list[float]]:
    """Feature vector for one corpus row, or None when it is unusable."""
    try:
        m15 = (json.loads(row.get("indicators_json") or "{}") or {}).get("M15") or {}
        fvg = json.loads(row.get("fvg_json") or "{}") or {}
    except Exception:
        return None
    if not m15:
        return None
    return _vector(row.get("direction") or "", m15.get("rsi14"), m15.get("adx14"),
                   m15.get("atr14"), row.get("regime_score"), fvg)


def _dataset() -> tuple[list, list, list, float]:
    """(X, y, sample_weight, rsi_spread) over the whole corpus."""
    X: list = []
    y: list = []
    w: list = []
    rsis: list[float] = []
    for background, label in ((False, 1), (True, 0)):
        for row in pro_corpus.rows(background=background):
            v = _row_vector(row)
            if v is None:
                continue
            X.append(v)
            y.append(label)
            w.append(1.0 if label == 0
                     else _OUTCOME_WEIGHT.get(row.get("outcome") or "", 1.0))
            rsis.append(v[FEATURE_NAMES.index("rsi_norm")] * 100.0)
    spread = (max(rsis) - min(rsis)) if rsis else 0.0
    return X, y, w, spread


# ── Fitting ───────────────────────────────────────────────────────────────────

def fit(force: bool = False) -> dict:
    """Refit from the current corpus. Returns the status dict (also cached).

    Cheap enough to call on every captured signal: a few hundred rows and
    twelve features. Skips the work when the corpus has not grown, so a
    caller that fires per signal does not refit on unchanged data.
    """
    X, y, w, spread = _dataset()
    n_pos = sum(1 for v in y if v == 1)
    n_neg = len(y) - n_pos

    if not force and _state["rows_at_fit"] == len(y) and _state["model"] is not None:
        return status()

    reasons = []
    if n_pos < _MIN_POSITIVES:
        reasons.append(f"only {n_pos}/{_MIN_POSITIVES} pro signals")
    if n_neg < _MIN_NEGATIVES:
        reasons.append(f"only {n_neg}/{_MIN_NEGATIVES} background samples")
    if spread < _MIN_RSI_SPREAD:
        reasons.append(f"RSI spread {spread:.0f} < {_MIN_RSI_SPREAD:.0f} (single-regime sample)")
    if reasons:
        _state.update(model=None, scaler=None, auc=None, n=len(y),
                      rows_at_fit=len(y), reason="; ".join(reasons))
        return status()

    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import StratifiedKFold
    except Exception as exc:
        _state.update(model=None, reason=f"sklearn unavailable: {exc}", rows_at_fit=len(y))
        return status()

    Xa, ya, wa = np.array(X, dtype=float), np.array(y), np.array(w, dtype=float)

    # A FOREST, NOT A REGRESSION. fvg_dist_norm carries 5.0 as the sentinel
    # for "no aligned gap anywhere", which is a category wearing a number's
    # clothes. A linear model extrapolates off the end of it: measured on the
    # real corpus, a moment with NO gap and no trend scored 0.96 pro-like
    # purely because 5.0 sat far outside the fitted range. A tree splits on it
    # instead, and the same moment reads 0.43 against 0.61 for a fresh aligned
    # gap -- the right way round. It also scores better honestly (0.827
    # held-out against 0.763).
    def _make():
        return RandomForestClassifier(n_estimators=300, min_samples_leaf=5,
                                      class_weight="balanced", random_state=42)

    # Out-of-fold AUC, not in-sample: with a few hundred rows an in-sample
    # score would look good on a model that has memorised the week, which is
    # the one failure mode this whole file is built to avoid.
    try:
        oof = np.zeros(len(ya), dtype=float)
        for tr, te in StratifiedKFold(n_splits=4, shuffle=True, random_state=42).split(Xa, ya):
            m = _make()
            m.fit(Xa[tr], ya[tr], sample_weight=wa[tr])
            oof[te] = m.predict_proba(Xa[te])[:, 1]
        auc = float(roc_auc_score(ya, oof))
    except Exception as exc:
        log.debug("[ProModel] AUC estimation failed: %s", exc)
        auc = 0.0

    model = _make()
    model.fit(Xa, ya, sample_weight=wa)

    _state.update(model=model, scaler=None, auc=auc, n=len(ya),
                  fitted_at=time.time(), rows_at_fit=len(ya),
                  reason="ok" if auc >= _MIN_AUC else
                         f"held-out AUC {auc:.3f} < {_MIN_AUC} — not used")
    log.info("[ProModel] fitted n=%d (pos=%d neg=%d) AUC=%.3f -> %s",
             len(ya), n_pos, n_neg, auc, _state["reason"])
    return status()


def _ready() -> bool:
    return (_state["model"] is not None and _state["auc"] is not None
            and _state["auc"] >= _MIN_AUC)


def pro_likeness(direction: str, rsi: Optional[float], adx: Optional[float],
                 atr: Optional[float], regime: Optional[float],
                 fvg: Optional[dict] = None) -> float:
    """P(a professional fires here), or NEUTRAL when the model is not
    trustworthy yet. Never raises: this is enrichment on the scoring path."""
    try:
        if not _ready():
            fit()
        if not _ready():
            return NEUTRAL
        v = _vector(direction, rsi, adx, atr, regime, fvg or {})
        if v is None:
            return NEUTRAL
        import numpy as np
        X = np.array(v, dtype=float).reshape(1, -1)
        return round(float(_state["model"].predict_proba(X)[0][1]), 4)
    except Exception as exc:
        log.debug("[ProModel] scoring failed: %s", exc)
        return NEUTRAL


def status() -> dict:
    c = {}
    try:
        c = pro_corpus.counts()
    except Exception:
        pass
    return {
        "ready": _ready(),
        "auc": _state["auc"],
        "n": _state["n"],
        "reason": _state["reason"],
        "fitted_at": _state["fitted_at"],
        "corpus": c,
        "min_auc": _MIN_AUC,
    }


def on_new_signal() -> None:
    """Called after a reference signal is captured, when the learning toggle
    is on. One incremental refit per signal is the whole point of the toggle;
    fit() itself is a no-op when the corpus has not actually grown."""
    try:
        fit()
    except Exception as exc:
        log.debug("[ProModel] refit failed: %s", exc)
