"""
ML engine for Reversal Engine signal generator.

Two learning axes:
  1. OUTCOME learning  — which of our signals win/lose (R-multiple regression)
  2. REF PATTERN learning — which level types the reference channel preferentially trades,
     used to update the level scoring function over time

Uses LightGBMRegressor + SGDRegressor; predicts R-multiple (positive = profitable).
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

from backend.src.services.reversal_engine.re_macro import (
    MACRO_FEATURE_NAMES, MACRO_NEUTRAL, macro_features)

_log = logging.getLogger(__name__)

_MIN_TRAIN = 15
_RETRAIN_EVERY = 5
# Public aliases — the UI panel reads these directly (matches breakout_signal/
# ml_engine.py's naming, which the Reversal Engine panel's ML section is ported from).
MIN_TRAIN_SAMPLES = _MIN_TRAIN
RETRAIN_EVERY = _RETRAIN_EVERY

_data_dir: Optional[Path] = None
_model_batch  = None   # LightGBM or RandomForest
_model_online = None   # SRElassifier
_labeled_count = 0
# Version history: docs/system/domains/engines/README.md (rationale, not code).
_version = "re_ml_v9"

from backend.src.services.reversal_engine import ml_handover as _ho  # noqa: E402


# _VIRTUAL_LOT / _DOLLARS_PER_POINT / _R_LABEL_CLAMP moved to _training_data.py
# with the label that uses them, and are re-exported further down. They were
# briefly defined in both places during the 2026-09-11 split, which is harmless
# only until one of them is edited.
_train_history: list[dict] = []

# REF pattern counters: level_type → {trades, wins, touches}
#   trades  = correlation matches recorded for this level type (existing semantics)
#   wins    = of those matches, how many were winners
#   touches = every time our engine *saw* a candidate of this level type, matched
#             or not — the true denominator needed to know if a level type is
#             genuinely REF-preferred vs. just frequently detected by our engine
_ref_level_stats: dict[str, dict] = {}


def init(data_dir: str) -> None:
    global _data_dir
    _data_dir = Path(data_dir)
    _load_all()


def _save_all() -> None:
    if _data_dir is None:
        return
    try:
        import joblib
        # record_ref_signal() saves too, and it can fire long before this
        # process has any labelled count of its own -- so never let an
        # in-memory 0 overwrite a real stored count (see
        # _labeled_count_from_db for the failure this caused live).
        labeled = _labeled_count
        if labeled == 0:
            try:
                prior = joblib.load(_data_dir / "re_ml_meta.pkl")
                if prior.get("version") == _version:
                    labeled = int(prior.get("labeled_count", 0) or 0)
            except Exception:
                pass
        meta = {
            "version":      _version,
            "labeled_count": labeled,
            "train_history": _train_history[-20:],
            "ref_level_stats": _ref_level_stats,
        }
        joblib.dump(meta, _data_dir / "re_ml_meta.pkl")
        if _model_batch is not None:
            joblib.dump(_model_batch, _data_dir / "re_ml_batch.pkl")
        if _model_online is not None:
            joblib.dump(_model_online, _data_dir / "re_ml_online.pkl")
    except Exception as exc:
        _log.debug("[RE-ML] save error: %s", exc)


def _load_all() -> None:
    global _model_batch, _model_online, _labeled_count, _train_history, _ref_level_stats
    if _data_dir is None:
        return
    try:
        import joblib
        meta_path = _data_dir / "re_ml_meta.pkl"
        if meta_path.exists():
            meta = joblib.load(meta_path)
            if meta.get("version") != _version:
                # Models and label-scale-dependent counters are discarded, but
                # `touches` is salvaged: it's a pure count of levels this engine
                # evaluated, independent of features and of the label, and it's
                # the denominator ref_match_rate_for_type needs (~11k observations
                # that would otherwise take weeks to rebuild).
                #
                # trades/wins are deliberately zeroed rather than salvaged. Up to
                # v4 `wins` was never incremented by anything (record_ref_signal
                # was only ever called with was_win=None), so every type carried a
                # non-zero `trades` against 0 `wins` and _ref_win_rate_for_type
                # returned a hard 0.0 for exactly the level types the reference
                # channel trades most. Zeroing both restores the neutral 0.65
                # prior until the now-wired credit-back populates them for real.
                _ref_level_stats = {
                    k: {"trades": 0, "wins": 0, "touches": v.get("touches", 0)}
                    for k, v in (meta.get("ref_level_stats") or {}).items()
                }
                if _ho.may_hand_over(meta.get("version")):
                    _model_batch, _model_online, _w = _ho.hand_over(_data_dir)
                    _ho.set_legacy_width(_w)
                    _log.warning("[RE-ML] %s -> %s — HANDING OVER, old model scores "
                                 "its first %s features (ml_handover)",
                                 meta.get("version"), _version, _w)
                    return
                _log.warning(
                    "[RE-ML] %s -> %s — discarding models: label epoch differs, so "
                    "the ML gate does NOT block until the first retrain. Kept "
                    "touches for %d types", meta.get("version"), _version,
                    len(_ref_level_stats))
                return
            _labeled_count  = meta.get("labeled_count", 0)
            _train_history  = meta.get("train_history", [])
            _ref_level_stats = meta.get("ref_level_stats", {})
        batch_path  = _data_dir / "re_ml_batch.pkl"
        online_path = _data_dir / "re_ml_online.pkl"
        if batch_path.exists():
            _model_batch = joblib.load(batch_path)
        if online_path.exists():
            _model_online = joblib.load(online_path)
        _log.info("[RE-ML] loaded — labeled=%d", _labeled_count)
    except Exception as exc:
        _log.debug("[RE-ML] load error: %s", exc)


# ── Feature extraction ────────────────────────────────────────────────────────

# The vector's shape lives in _feature_schema.py: two modules need it (this one
# to build a vector, _training_data.py to right-pad a historical one) and it
# carries no state. Re-exported here because every caller and every test reads
# `ml_engine.FEATURE_NAMES`.
from ._feature_schema import (  # noqa: E402
    _LEVEL_TYPES, FEATURE_NAMES, _FEATURE_NEUTRAL,
)


def learning_from_ref_enabled() -> bool:
    """The Signal Generator > Reversal toggle. Off (the default) means the
    Reversal Engine ignores the pro-likeness model entirely -- the feature
    stays at its neutral, and nothing refits on an incoming signal."""
    try:
        from backend.src.db import database as _cdb
        return bool(_cdb.get_risk_settings().get("re_learn_from_ref_signals", 0))
    except Exception:
        return False


def _pro_likeness_feature(signal_data: dict) -> float:
    """pro_model's verdict for this moment, or the neutral when the toggle is
    off or the model is not trustworthy. Never raises -- a research model
    must not be able to stop a signal being scored."""
    if not learning_from_ref_enabled():
        return 0.5
    try:
        from backend.src.services.reversal_engine import pro_model
        return pro_model.pro_likeness(
            signal_data.get("direction", "BUY"),
            signal_data.get("rsi14"),
            signal_data.get("adx"),
            signal_data.get("atr"),
            signal_data.get("regime_score"),
            {
                "fvg_confluence": signal_data.get("fvg_confluence"),
                "fvg_dist_norm":  signal_data.get("fvg_dist_norm"),
                "fvg_fresh":      signal_data.get("fvg_fresh"),
                "fvg_size_norm":  signal_data.get("fvg_size_norm"),
            },
        )
    except Exception as exc:
        _log.debug("[RE-ML] pro_likeness error: %s", exc)
        return 0.5


def _pro_features(signal_data: dict) -> list[float]:
    """The four pro-profile values, in FEATURE_NAMES order. Never raises:
    this is enrichment, and a failure here must not stop a signal being
    scored at all."""
    try:
        from backend.src.services.reversal_engine.pro_profile import profile_features
        f = profile_features(
            signal_data.get("direction", "BUY"),
            signal_data.get("rsi14"),
            signal_data.get("adx"),
            signal_data.get("fvg_confluence"),
        )
    except Exception:
        f = {}
    return [float(f.get("pro_rsi_delta") or 0.0),
            float(f.get("pro_adx_delta") or 0.0),
            float(f.get("pro_fvg_delta") or 0.0),
            float(f.get("pro_profile_ready") or 0.0)]


def extract_features(signal_data: dict, recent_win_rate: float = 0.5) -> Optional[list[float]]:
    """Extract feature vector from a signal dict (len(FEATURE_NAMES) elements)."""
    try:
        level_type  = signal_data.get("level_type", "")
        level_score = float(signal_data.get("level_score", 0.5) or 0.5)
        htf_bias    = signal_data.get("htf_bias", "neutral")
        direction   = signal_data.get("direction", "BUY")
        session     = signal_data.get("session", "off")
        adx         = float(signal_data.get("adx", 0) or 0)
        atr         = float(signal_data.get("atr", 8) or 8)
        rr_tp1      = float(signal_data.get("rr_tp1", 0.75) or 0.75)
        created_at  = float(signal_data.get("created_at", time.time()) or time.time())
        distance    = float(signal_data.get("distance_pts",
                            abs(float(signal_data.get("price_at_signal", 0) or 0)
                                - float(signal_data.get("level_price", 0) or 0))) or 5)

        hour = time.gmtime(created_at).tm_hour

        is_asia   = 1 if level_type in ("asia_low", "asia_high") else 0
        is_swing  = 1 if "swing" in level_type else 0
        is_round  = 1 if "round" in level_type else 0

        htf_map   = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
        dir_map   = {"BUY": 1.0, "SELL": -1.0}
        sess_map  = {"overlap": 1.5, "london": 1.0, "ny": 0.8, "asian": 0.5, "off": 0.0}

        htf_score  = htf_map.get(htf_bias, 0.0)
        dir_score  = dir_map.get(direction, 1.0)
        bias_align = 1.0 if htf_score * dir_score > 0 else 0.0
        sess_score = sess_map.get(session, 0.0)

        adx_norm  = min(adx / 50.0, 1.0)
        atr_norm  = min(atr / 20.0, 1.0)
        dist_norm = min(distance / max(atr, 1.0), 5.0)
        h_sin     = math.sin(hour * 2 * math.pi / 24)
        h_cos     = math.cos(hour * 2 * math.pi / 24)

        # REF win rate for this level type
        ref_wr = _ref_win_rate_for_type(level_type)

        # Cadence — how "due" the reference channel is to post, based on real signal timing.
        # Populated by engine.py once per cycle from vantage_tg_signals; default
        # to a neutral/unfavourable prior (4h since last signal, 0 today) so
        # signals built without this context don't get an artificial boost.
        mins_since_ref = float(signal_data.get("minutes_since_last_ref", 240) or 240)
        ref_today      = float(signal_data.get("ref_signals_today", 0) or 0)
        mins_since_norm = min(mins_since_ref / 240.0, 1.0)
        ref_today_norm  = min(ref_today / 10.0, 1.0)

        return [
            level_score,
            is_asia,
            is_swing,
            is_round,
            htf_score,
            dir_score,
            bias_align,
            sess_score,
            adx_norm,
            atr_norm,
            h_sin,
            h_cos,
            dist_norm,
            recent_win_rate,
            ref_wr,
            min(rr_tp1, 5.0),
            mins_since_norm,
            ref_today_norm,
            float(signal_data.get("news_proximity_norm") or 1.0),   # news_proximity_norm
            float(signal_data.get("regime_score")        or 0.5),   # regime_score
            float(signal_data.get("equity_drawdown_pct") or 0.0),   # equity_drawdown_pct
            float(signal_data.get("concurrent_agreement")or 0.0),   # concurrent_agreement
            float(signal_data.get("ref_discipline_score") or 0.5),  # ref_discipline_score
            float(signal_data.get("ref_aggression_score") or 0.5),  # ref_aggression_score
            # FVG context. Supplied by the caller (reversal_engine_service
            # builds it from ict_patterns.fvg_context on the M15 candles it
            # already has). Absent -> the documented "no gap found"
            # neutrals, so a signal generated without FVG context is never
            # mistaken for one sitting in a fresh gap.
            float(signal_data.get("fvg_confluence") or 0.0),
            float(signal_data.get("fvg_dist_norm", 5.0) or 5.0),
            float(signal_data.get("fvg_fresh", 0.5) if signal_data.get("fvg_fresh") is not None else 0.5),
            float(signal_data.get("fvg_size_norm") or 0.0),
            # Reference-channel structure. Computed here rather than by the
            # caller so every path gets it automatically, and so a missing
            # profile degrades to the documented neutrals.
            *_pro_features(signal_data),
            _pro_likeness_feature(signal_data),
            # Macro. The caller injects the raw series into signal_data;
            # absent, these are the documented neutrals.
            *macro_features(signal_data, None),
        ]
    except Exception as exc:
        _log.debug("[RE-ML] extract_features error: %s", exc)
        return None


def _ref_win_rate_for_type(level_type: str) -> float:
    """Return historical REF win rate for a given level type (from REF pattern learning)."""
    stats = _ref_level_stats.get(level_type)
    if not stats or stats.get("trades", 0) < 3:
        return 0.65  # prior: the reference channel has ~66% historical WR
    return stats["wins"] / stats["trades"]


def ref_match_rate_for_type(level_type: str) -> Optional[float]:
    """Of every time we *saw* a candidate of this level type (touches), what
    fraction actually correlated with a real REF signal (trades). This is the
    true precision signal — distinct from _ref_win_rate_for_type, which only
    looks at outcome quality among matches, not how often this level type
    predicts a REF signal will appear at all. Returns None until enough data."""
    stats = _ref_level_stats.get(level_type)
    if not stats or stats.get("touches", 0) < 5:
        return None
    return stats["trades"] / stats["touches"]


# ── Training ──────────────────────────────────────────────────────────────────

# The label and the training set live in _training_data.py -- no model state,
# no globals, so they move cleanly. Re-exported: the label tests read
# `ml_engine._realised_r`.
from ._training_data import (  # noqa: E402
    _VIRTUAL_LOT, _DOLLARS_PER_POINT, _R_LABEL_CLAMP,
    _realised_r, _get_training_data, _labeled_count_from_db,
)


def _retrain() -> None:
    global _model_batch, _labeled_count, _train_history
    X, y = _get_training_data()
    if len(X) < _MIN_TRAIN:
        return

    import numpy as np
    Xa = np.array(X, dtype=float)
    ya = np.array(y, dtype=float)

    # Time-decay weights
    weights = np.array([math.exp(-0.017 * (len(X) - i)) for i in range(len(X))])
    weights = weights / weights.sum() * len(weights)

    # Try LightGBM regressor, fall back to RandomForestRegressor
    try:
        import lightgbm as lgb
        _model_batch = lgb.LGBMRegressor(
            n_estimators=100, learning_rate=0.05, num_leaves=15,
            min_child_samples=5, random_state=42, verbose=-1,
        )
        _model_batch.fit(Xa, ya, sample_weight=weights)
        backend = "lgb"
    except ImportError:
        from sklearn.ensemble import RandomForestRegressor
        _model_batch = RandomForestRegressor(
            n_estimators=80, max_depth=5, random_state=42
        )
        _model_batch.fit(Xa, ya, sample_weight=weights)
        backend = "rf"

    _labeled_count = len(X)
    _ho.end()
    _train_history.append({
        "ts": time.time(), "n": len(X), "mean_r": round(float(np.mean(ya)), 4), "backend": backend
    })
    _save_all()
    _log.info("[RE-ML] retrained — n=%d backend=%s", len(X), backend)


def retrain_now() -> None:
    """Public wrapper so callers outside this module (telegram_research.py's
    nightly job) can force an immediate retrain rather than waiting for the
    next _RETRAIN_EVERY outcome — used right after the daily discipline/
    aggression scores update so the model reflects them without delay."""
    _retrain()


def get_daily_research_scores() -> tuple[float, float]:
    """Cached (discipline_score, aggression_score) from the most recent
    nightly Telegram research run, read at signal-generation time. Neutral
    0.5/0.5 prior until the first research run has completed."""
    try:
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        d = float(re_db.get_config("ref_discipline_score", "0.5") or 0.5)
        a = float(re_db.get_config("ref_aggression_score", "0.5") or 0.5)
        return d, a
    except Exception:
        return 0.5, 0.5


def predict(features: list) -> Optional[float]:
    """Return predicted R-multiple or None if not trained.
    Positive = expected profitable, negative = expected losing.
    Blends batch model (60%) + online SGD (40%) when both available."""
    global _model_online, _model_batch
    if features is None:
        return None

    import numpy as np
    features = _ho.truncate(features)
    fa = np.array(features, dtype=float).reshape(1, -1)
    preds = []

    if _model_batch is not None:
        try:
            p = float(_model_batch.predict(fa)[0])
            preds.append((p, 0.6))
        except Exception:
            pass

    if _model_online is not None:
        try:
            p = float(_model_online.predict(fa)[0])
            preds.append((p, 0.4))
        except Exception:
            pass

    if not preds:
        return None

    total_w = sum(w for _, w in preds)
    return round(sum(p * w for p, w in preds) / total_w, 4)


def record_outcome(signal_id: int, outcome: str) -> None:
    """Called when a signal closes — update online learner + maybe retrain batch."""
    global _model_online, _labeled_count

    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    sig = re_db.get_signal_by_id(signal_id)
    if not sig:
        return

    # Credit this outcome back to the REF level type it correlated with, so
    # ref_level_win_rate reflects how the reference channel's preferred levels
    # actually resolve. Done before the feature/label guards below so a signal
    # that can't be trained on still updates the pattern stats.
    ref_level_type = sig.get("correlated_ref_level_type")
    if ref_level_type and sig.get("correlation_confirmed"):
        record_ref_signal(ref_level_type, was_win=(outcome == "win"))

    feats_json = sig.get("ml_features_json")
    if not feats_json:
        return
    try:
        feats = json.loads(feats_json)
    except Exception:
        return
    if len(feats) != len(FEATURE_NAMES):
        return  # stored under an older feature set — dimensions won't line up

    label = _realised_r(sig)
    if label is None:
        _log.debug("[RE-ML] signal=%s has no realised R (sl_dist/net_pnl missing) — "
                   "not used for training", signal_id)
        return

    # Self-heal a counter that was clobbered by a save from a process which
    # never had it (see _labeled_count_from_db). Done here rather than at
    # init() because the reversal-engine DB isn't guaranteed to be wired up
    # that early, whereas by this point we have just read a signal from it.
    if _labeled_count == 0:
        _labeled_count = _labeled_count_from_db()

    import numpy as np
    fa = np.array(feats, dtype=float).reshape(1, -1)

    # Online update — SGDRegressor with huber loss
    if _model_online is None:
        from sklearn.linear_model import SGDRegressor
        _model_online = SGDRegressor(
            loss="huber", epsilon=0.1, random_state=42
        )
    # Check if saved model is an old classifier and reset it
    if hasattr(_model_online, "classes_"):
        _log.info("[RE-ML] Online model is old classifier — resetting to SGDRegressor")
        from sklearn.linear_model import SGDRegressor
        _model_online = SGDRegressor(
            loss="huber", epsilon=0.1, random_state=42
        )
    try:
        # Unweighted as of v5. The old 2x weight on losing samples existed to
        # compensate for a label that priced every loss at a flat -1.0R; the
        # realised-R label now carries that magnitude itself (losses average
        # -1.22R and reach -5.75R), so keeping the multiplier would count the
        # same asymmetry twice and over-penalise every loser.
        _model_online.partial_fit(fa, [label])
    except Exception:
        pass

    _labeled_count += 1

    # Batch retrain
    if _labeled_count % _RETRAIN_EVERY == 0 and _labeled_count >= _MIN_TRAIN:
        _retrain()
    else:
        _save_all()


def record_ref_signal(level_type: str, was_win: Optional[bool] = None) -> None:
    """
    Update REF level pattern stats when a the reference channel signal is observed.
    This is how the engine learns which level types the real trader prefers.
    was_win=None when signal arrives (outcome unknown yet), True/False when closed.
    """
    global _ref_level_stats
    if level_type not in _ref_level_stats:
        _ref_level_stats[level_type] = {"trades": 0, "wins": 0, "touches": 0}
    _ref_level_stats[level_type].setdefault("touches", 0)

    if was_win is None:
        _ref_level_stats[level_type]["trades"] += 1
    elif was_win is True:
        _ref_level_stats[level_type]["wins"] += 1

    # Persist
    _save_all()


def record_level_touch(level_type: str) -> None:
    """Called once per cycle for every candidate level our engine evaluates,
    matched or not. Builds the true denominator for ref_match_rate_for_type —
    without this, a level type that's simply detected often looks identical
    to one REF actually trades often, since both currently only count matches.
    Not persisted on every call (cheap in-memory increment); flushed by the
    next _save_all() triggered elsewhere (record_ref_signal/record_outcome)."""
    global _ref_level_stats
    if level_type not in _ref_level_stats:
        _ref_level_stats[level_type] = {"trades": 0, "wins": 0, "touches": 0}
    _ref_level_stats[level_type].setdefault("touches", 0)
    _ref_level_stats[level_type]["touches"] += 1


def is_trained() -> bool:
    return _model_batch is not None or _model_online is not None


def summary() -> dict:
    return {
        "trained":       is_trained(),
        "labeled_count": _labeled_count,
        "min_needed":    _MIN_TRAIN,
        "has_batch":     _model_batch is not None,
        "has_online":    _model_online is not None,
        "train_history": _train_history[-5:],
        "ref_level_stats": _ref_level_stats,
        "features":      FEATURE_NAMES,
        "n_features":    len(FEATURE_NAMES),
    }


def get_ml_metrics() -> dict:
    """Regression learning metrics: mean predicted R, mean actual R, directional
    accuracy — ported from breakout_signal/ml_engine.py's get_ml_metrics() for
    UI parity (feeds the "Is it learning?" chart in the Reversal Engine ML panel,
    previously missing entirely)."""
    _blank = {
        "n_data":           0,
        "win_rate_series":  [],
        "pred_r_series":    [],
        "actual_r_series":  [],
        "signal_ids":       [],
        "mean_pred_r":      None,
        "mean_actual_r":    None,
        "accuracy":         None,
        "train_history":    _train_history,
        "labeled_count":    _labeled_count,
    }
    try:
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        rows = re_db.fetch_ml_outcome_rows()
        if not rows:
            return _blank

        signal_ids, pred_rs, actual_rs, actuals_win = [], [], [], []
        for row in rows:
            outcome = (row["outcome"] or "").lower()
            if outcome not in ("win", "loss", "be"):
                continue
            # Same realised-R definition the model is now trained on, so the
            # panel's "mean actual R" matches the money in re_balance_log
            # instead of the planned-target fiction it used to plot.
            actual_r = _realised_r(dict(row))
            if actual_r is None:
                continue
            signal_ids.append(row["signal_ref"] or str(row["id"]))
            pred_rs.append(float(row["ml_prob"]))
            actual_rs.append(actual_r)
            actuals_win.append(1 if outcome == "win" else 0)

        if not signal_ids:
            return _blank

        n = len(signal_ids)
        mean_pred_r   = round(sum(pred_rs) / n, 4)
        mean_actual_r = round(sum(actual_rs) / n, 4)
        correct = sum(1 for p, a in zip(pred_rs, actual_rs) if (p >= 0) == (a >= 0))
        accuracy = round(correct / n, 4) if n > 0 else None

        win_rate_series: list = []
        wins = 0
        for i, a in enumerate(actuals_win):
            wins += a
            win_rate_series.append(round(wins / (i + 1) * 100, 1))

        return {
            "n_data":           n,
            "win_rate_series":  win_rate_series,
            "pred_r_series":    [round(p, 3) for p in pred_rs],
            "actual_r_series":  [round(r, 1) for r in actual_rs],
            "signal_ids":       signal_ids,
            "mean_pred_r":      mean_pred_r,
            "mean_actual_r":    mean_actual_r,
            "accuracy":         accuracy,
            "train_history":    _train_history,
            "labeled_count":    _labeled_count,
        }
    except Exception as e:
        _log.debug("[RE-ML] metrics error: %s", e)
        return _blank
