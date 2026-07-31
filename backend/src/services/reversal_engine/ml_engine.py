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
# v3 switches to R-multiple regression and adds 4 new features (news_proximity_norm,
# regime_score, equity_drawdown_pct, concurrent_agreement) — discards v2 models so
# dimension and label format mismatches can't happen; retrains from scratch.
# v4 adds ref_discipline_score/ref_aggression_score — daily values derived by
# telegram_research.py's nightly AI read of the reference channel/GD2 messages+images, cached
# in re_config and refreshed once per night. Same discard-and-retrain-from-
# scratch handling as v3 for the same reason (dimension mismatch).
_version = "re_ml_v4"
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
        meta = {
            "version":      _version,
            "labeled_count": _labeled_count,
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
                _log.info("[RE-ML] version mismatch — discarding saved models")
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
]


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

def _get_training_data():
    """Pull closed signals with features from DB. Returns (X, y) where y is R-multiple."""
    try:
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        rows = re_db.get_ml_training_data()
        X, y = [], []
        for r in rows:
            feats = r.get("ml_features_json")
            if not feats:
                continue
            try:
                f = json.loads(feats)
            except Exception:
                continue
            # Guards against a ragged X array when older rows were labeled
            # under a previous _version with a different feature count (e.g.
            # pre-v4 rows missing ref_discipline_score/ref_aggression_score).
            if len(f) != len(FEATURE_NAMES):
                continue
            outcome = r.get("outcome", "")
            if outcome not in ("win", "loss", "be"):
                continue
            _rr = float(r.get("rr_tp1") or 1.0)
            label = _rr if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)
            X.append(f)
            y.append(label)
        return X, y
    except Exception as exc:
        _log.debug("[RE-ML] training data error: %s", exc)
        return [], []


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

    feats_json = sig.get("ml_features_json")
    if not feats_json:
        return
    try:
        feats = json.loads(feats_json)
    except Exception:
        return

    import numpy as np
    fa = np.array(feats, dtype=float).reshape(1, -1)

    rr_tp1 = float((sig or {}).get("rr_tp1") or 1.0)
    label = rr_tp1 if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)

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
        sw = [2.0 if label < 0 else 1.0]
        _model_online.partial_fit(fa, [label], sample_weight=sw)
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
        rows = re_db.get_db().all(
            "SELECT id, signal_ref, ml_prob, outcome, rr_tp1 "
            "FROM re_signals "
            "WHERE ml_prob IS NOT NULL AND outcome IS NOT NULL AND outcome != 'open' "
            "ORDER BY id"
        )
        if not rows:
            return _blank

        signal_ids, pred_rs, actual_rs, actuals_win = [], [], [], []
        for row in rows:
            outcome = (row["outcome"] or "").lower()
            if outcome not in ("win", "loss", "be"):
                continue
            rr_tp1 = float(row["rr_tp1"] or 1.0)
            actual_r = rr_tp1 if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)
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
