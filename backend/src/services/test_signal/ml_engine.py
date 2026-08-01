"""
Persistent ML signal quality predictor for XAUUSD.

Design:
  - Primary model: LightGBM (fast, small-data friendly) with Random Forest fallback
  - Regime-aware: separate trending / ranging models plus a combined fallback
  - Walk-forward update with exponential time-decay (recent trades weighted higher)
  - Online learning: SGDRegressor updated per-trade between batch retrains
  - Cold-start: rules-based only until MIN_TRAIN_SAMPLES available
  - Persistent: models saved to disk via joblib; survive restarts
  - Target: R-multiple regression (rr_tp1 for win, -1.0 for loss, 0.0 for BE)

Feature groups (42 total):
  1. Momentum/oscillator  — RSI level, slope, divergence
  2. Volatility structure  — ATR ratio, candle body/wick geometry
  3. Candle patterns       — engulfing, pin bar, consecutive run
  4. Level context         — strength, confluence, distance in ATR
  5. Signal quality        — R:R, SL sizing
  6. Market context        — HTF bias, H4 bias, session, cyclic time
  7. Adaptive history      — recent win rate
  8. Indicators            — ADX, MACD histogram, regime
  9. Volume                — current vs average volume, volume at level
 10. External context      — DXY momentum, US 10Y yield, VIX
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger("test_signal")

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    import numpy as np
    _NP = True
except ImportError:
    _NP = False

def _import_lightgbm():
    """Import lightgbm, auto-installing libomp on macOS if the dylib is missing.

    On macOS, lightgbm's native library requires libomp (OpenMP) which is not
    bundled with the pip wheel and must come from Homebrew.  If the first import
    fails with OSError (dylib not found), this function silently runs
    'brew install libomp', clears the partial import cache, and retries once.
    Falls back to None if Homebrew is absent or the retry also fails.
    """
    import sys as _sys
    try:
        import lightgbm as _lgb
        return _lgb, True
    except (ImportError, OSError) as _err:
        if not (_sys.platform == "darwin" and isinstance(_err, OSError)):
            return None, False

    # macOS OSError — libomp missing.  Try to install via Homebrew.
    import os as _os, subprocess as _sp
    _brew = next(
        (b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
         if _os.path.isfile(b)),
        None,
    )
    if _brew is None:
        return None, False

    print("  Installing OpenMP runtime (libomp) required by LightGBM — please wait...")
    _sp.run([_brew, "install", "libomp", "--quiet"],
            capture_output=True, timeout=300, check=False)

    # Clear any partial module cache entries left by the failed import
    for _k in list(_sys.modules):
        if _k == "lightgbm" or _k.startswith("lightgbm."):
            del _sys.modules[_k]

    try:
        import lightgbm as _lgb
        print("  LightGBM loaded successfully.")
        return _lgb, True
    except (ImportError, OSError):
        return None, False


lgb, _LGB = _import_lightgbm()

try:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.linear_model import SGDRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import cross_val_score
    import joblib
    _SK = True
except ImportError:
    _SK = False

_ML_AVAILABLE = _NP and _SK

# ── Constants ─────────────────────────────────────────────────────────────────
MIN_TRAIN_SAMPLES  = 20    # batch model needs this many labeled examples
MIN_REGIME_SAMPLES = 20    # minimum per-regime before training that model
RETRAIN_EVERY      = 5     # retrain every N new labeled examples after MIN_TRAIN_SAMPLES
BOOTSTRAP_SAMPLES  = 20    # bootstrap mode ends at this count
DECAY_HALFLIFE     = 60    # exponential time-decay half-life in number of trades

from backend.src.services.test_signal.ml_features import (  # noqa: F401
    FEATURE_NAMES, extract_features, to_vector,
    _count_consecutive, _detect_divergence, _ema, _get_recent_win_rate,
    _rsi, _vol_features,
)

MODEL_VERSION = len(FEATURE_NAMES)  # invalidates saved models on feature changes

# ── Module-level state ────────────────────────────────────────────────────────
_model:          Optional[object] = None   # combined batch model
_model_trending: Optional[object] = None   # regime: trending (ADX >= 25)
_model_ranging:  Optional[object] = None   # regime: ranging  (ADX <  25)
_online_model:   Optional[object] = None   # SGD online model
_batch_scaler:   Optional[object] = None   # StandardScaler fitted on batch data
_model_dir:      Optional[Path]   = None
_n_since_last_train: int = 0

# Monitoring state — populated on each batch retrain
_train_history:  list              = []    # [{ts, n_samples, cv_auc, feature_importances}]
_train_centroid: Optional[list]    = None  # mean feature vector of training set
_train_std:      Optional[list]    = None  # std feature vector (for OOD normalisation)


def init(data_dir: Path) -> None:
    global _model_dir
    _model_dir = data_dir
    _load_all()
    backend = "LightGBM" if _LGB else "RandomForest"
    _log.info("[ML] Engine init. model=%s  backend=%s  sklearn=%s",
              "loaded" if _model else "cold-start", backend, _ML_AVAILABLE)


# ── Prediction ────────────────────────────────────────────────────────────────

def predict(features: dict) -> Optional[float]:
    """
    Return win-probability 0.0-1.0, or None if no model trained yet.
    Routes to regime-specific model when available; blends online signal.
    """
    if not _ML_AVAILABLE:
        return None
    X_vec = to_vector(features)

    # Choose regime model based on ADX
    adx_norm = features.get("adx_norm", 0.5)
    regime_model = _model_trending if (adx_norm >= 0.5 and _model_trending is not None) \
                   else _model_ranging if (adx_norm < 0.5 and _model_ranging is not None) \
                   else _model

    if regime_model is None:
        return None

    try:
        import numpy as np
        X = np.array([X_vec], dtype=float)
        batch_r = float(regime_model.predict(X)[0])
    except Exception as e:
        _log.debug("[ML] batch predict error: %s", e)
        return None

    # Blend online model if trained
    if _online_model is not None and _batch_scaler is not None:
        try:
            import numpy as np
            X_scaled = _batch_scaler.transform(np.array([X_vec], dtype=float))
            online_r = float(_online_model.predict(X_scaled)[0])
            blended = 0.7 * batch_r + 0.3 * online_r
            return round(blended, 4)
        except Exception:
            pass

    return round(batch_r, 4)


# ── Online learning ───────────────────────────────────────────────────────────

def record_outcome(signal_id: int, outcome: str) -> None:
    """Call after every signal closes. Triggers batch retrain when ready."""
    global _n_since_last_train
    _n_since_last_train += 1

    from backend.src.services.test_signal import test_signal_repo as _tdb
    total = len(_tdb.get_ml_training_data())
    _log.debug("[ML] Outcome %s for SIG-%04d (labeled=%d since_train=%d)",
               outcome, signal_id, total, _n_since_last_train)

    # Online update -- immediate, per-trade
    _online_update(signal_id, outcome)

    # Batch retrain when enough data and enough new examples
    if total >= MIN_TRAIN_SAMPLES and _n_since_last_train >= RETRAIN_EVERY:
        _retrain()
        _n_since_last_train = 0


def _online_update(signal_id: int, outcome: str) -> None:
    """
    Update the SGD online model with a single new example.
    Requires the batch scaler to be fitted first; skips silently if not.
    """
    global _online_model
    if not _ML_AVAILABLE or _batch_scaler is None:
        return

    from backend.src.services.test_signal import test_signal_repo as _tdb
    feat = _tdb.get_ml_features_for_signal(signal_id)
    if not feat:
        return

    sig_row = _tdb.get_signal_by_id(signal_id)
    rr_tp1  = float(sig_row.get("rr_tp1") or 1.0) if sig_row else 1.0
    label   = rr_tp1 if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)
    try:
        import numpy as np
        X_vec    = np.array([to_vector(feat)], dtype=float)
        X_scaled = _batch_scaler.transform(X_vec)

        if _online_model is None:
            _online_model = SGDRegressor(
                loss="huber", epsilon=0.1, max_iter=1, warm_start=True, random_state=42,
            )
        _online_model.partial_fit(X_scaled, [label])

        _log.debug("[ML] Online update: SIG-%04d outcome=%s", signal_id, outcome)
    except Exception as e:
        _log.debug("[ML] Online update error: %s", e)


# ── Batch retraining ──────────────────────────────────────────────────────────

def _build_regressor():
    """Return the best available regressor predicting R-multiple."""
    if _LGB:
        import lightgbm as lgb
        return lgb.LGBMRegressor(
            n_estimators=300,
            learning_rate=0.05,
            max_depth=5,
            min_child_samples=5,
            subsample=0.8,
            colsample_bytree=0.8,
            objective="regression",
            metric="l2",
            random_state=42,
            verbose=-1,
        )
    return RandomForestRegressor(
        n_estimators=200, max_depth=6, min_samples_leaf=3,
        random_state=42, n_jobs=-1,
    )


def _build_pipeline():
    return Pipeline([
        ("scaler", StandardScaler()),
        ("reg", _build_regressor()),
    ])


def _time_decay_weights(timestamps: list[float], halflife_n: int = DECAY_HALFLIFE) -> list[float]:
    """
    Exponential decay: the most recent trade has weight 1.0;
    a trade `halflife_n` trades ago has weight 0.5.
    """
    n = len(timestamps)
    weights = [2 ** (-(n - 1 - i) / halflife_n) for i in range(n)]
    total = sum(weights)
    return [w / total * n for w in weights]   # normalise so sum = n


def _retrain() -> None:
    global _model, _model_trending, _model_ranging, _batch_scaler, _train_centroid, _train_std
    if not _ML_AVAILABLE:
        return

    from backend.src.services.test_signal import test_signal_repo as _tdb
    examples = _tdb.get_ml_training_data()

    X_all, y_all, ts_all = [], [], []
    for ex in examples:
        feat = ex.get("features")
        out  = ex.get("outcome")
        if not feat or out is None:
            continue
        rr    = float(ex.get("rr_tp1", 1.0))
        label = rr if out == "win" else (-1.0 if out == "loss" else 0.0)
        X_all.append([float(feat.get(k, 0.0)) for k in FEATURE_NAMES])
        y_all.append(label)
        ts_all.append(float(ex.get("ts", time.time())))

    if len(X_all) < MIN_TRAIN_SAMPLES:
        _log.info("[ML] Skipping retrain: %d examples", len(X_all))
        return

    import numpy as np
    X_arr  = np.array(X_all, dtype=float)
    y_arr  = np.array(y_all, dtype=float)
    w_arr  = np.array(_time_decay_weights(ts_all), dtype=float)

    # ── Combined model ────────────────────────────────────────────────────────
    pipe = _build_pipeline()
    cv_n = min(5, len(X_all) // 10 + 1)
    cv_scores = cross_val_score(pipe, X_arr, y_arr, cv=cv_n, scoring="neg_mean_squared_error")
    cv_rmse = float(np.sqrt(-np.mean(cv_scores)))
    pipe.fit(X_arr, y_arr, **{"reg__sample_weight": w_arr} if _LGB else {})
    _model = pipe

    # Fit a standalone scaler for the online model
    scaler = StandardScaler()
    scaler.fit(X_arr)
    _batch_scaler = scaler

    _log.info("[ML] Retrained combined on %d examples. CV-RMSE=%.3f. Backend=%s",
              len(X_all), cv_rmse, "LightGBM" if _LGB else "RandomForest")

    # ── Regime-aware models ────────────────────────────────────────────────────
    adx_col = FEATURE_NAMES.index("adx_norm")
    trend_mask = X_arr[:, adx_col] >= 0.5
    range_mask = ~trend_mask

    for label_str, mask, setter in [
        ("trending", trend_mask, "_model_trending"),
        ("ranging",  range_mask, "_model_ranging"),
    ]:
        Xr, yr, wr = X_arr[mask], y_arr[mask], w_arr[mask]
        if len(Xr) >= MIN_REGIME_SAMPLES and len(set(yr.tolist())) >= 2:
            rp = _build_pipeline()
            rp.fit(Xr, yr, **{"reg__sample_weight": wr} if _LGB else {})
            if label_str == "trending":
                _model_trending = rp
            else:
                _model_ranging = rp
            _log.info("[ML] Retrained %s model on %d examples", label_str, len(Xr))

    # ── OOD centroid (training distribution centre for future dissimilarity checks) ─
    _train_centroid = [float(v) for v in X_arr.mean(axis=0)]
    _train_std      = [float(v) for v in (X_arr.std(axis=0) + 1e-9)]

    # ── Feature importances + history record ──────────────────────────────────
    fi: dict = {}
    try:
        clf = pipe.named_steps["reg"]
        imps = clf.feature_importances_
        fi   = {n: round(float(v), 5) for n, v in zip(FEATURE_NAMES, imps)}
        pairs = sorted(fi.items(), key=lambda x: -x[1])
        top5  = ", ".join(f"{n}={v:.3f}" for n, v in pairs[:5])
        wins = sum(1 for y in y_all if y > 0)
        _log.info("[ML] Top features: %s  (%dW/%dL)", top5, wins, len(y_all) - wins)
    except Exception:
        pass

    _train_history.append({
        "ts":                  time.time(),
        "n_samples":           len(X_all),
        "cv_auc":              round(cv_rmse, 4),   # field repurposed: stores CV-RMSE
        "feature_importances": fi,
    })

    _save_all()

    try:
        from backend.src.services.test_signal import test_signal_repo as _tdb2
        _tdb2.log_analysis({
            "ts":     time.time(),
            "result": f"ml_retrain:{len(X_all)}_samples",
            "claude_decision": (
                f"{'LGB' if _LGB else 'RF'} retrained on {len(X_all)} examples. "
                f"CV-AUC={cv_rmse:.3f}. Regime models: "
                f"trending={_model_trending is not None}, ranging={_model_ranging is not None}."
            ),
        })
    except Exception:
        pass


# ── Persistence ───────────────────────────────────────────────────────────────

def _model_path(name: str) -> Optional[Path]:
    if _model_dir is None:
        return None
    return _model_dir / f"ml_signal_{name}.joblib"


def _save_all() -> None:
    if not _ML_AVAILABLE or _model_dir is None:
        return
    for name, obj in [
        ("combined", _model),
        ("trending", _model_trending),
        ("ranging",  _model_ranging),
        ("scaler",   _batch_scaler),
    ]:
        if obj is None:
            continue
        path = _model_path(name)
        try:
            joblib.dump(obj, path)
        except Exception as e:
            _log.warning("[ML] Save %s failed: %s", name, e)
    _log.info("[ML] Models saved to %s", _model_dir)


def _load_all() -> None:
    global _model, _model_trending, _model_ranging, _batch_scaler
    if not _ML_AVAILABLE or _model_dir is None:
        return
    for name, setter in [
        ("combined", "_model"),
        ("trending", "_model_trending"),
        ("ranging",  "_model_ranging"),
        ("scaler",   "_batch_scaler"),
    ]:
        path = _model_path(name)
        if path is None or not path.exists():
            continue
        try:
            obj = joblib.load(path)
            # Version check for model objects (not scaler)
            if name != "scaler":
                reg = getattr(obj, "named_steps", {}).get("reg")
                if reg is None:
                    _log.warning("[ML] Discarding %s: old classifier pipeline (no 'reg' step)", name)
                    continue
                if hasattr(reg, "n_features_in_") and reg.n_features_in_ != MODEL_VERSION:
                    _log.warning("[ML] Discarding %s model: feature count mismatch (%d vs %d)",
                                 name, reg.n_features_in_, MODEL_VERSION)
                    continue
            if name == "combined":
                _model = obj
            elif name == "trending":
                _model_trending = obj
            elif name == "ranging":
                _model_ranging = obj
            elif name == "scaler":
                _batch_scaler = obj
        except Exception as e:
            _log.warning("[ML] Load %s failed: %s", name, e)
    if _model:
        _log.info("[ML] Loaded models from %s", _model_dir)


def is_trained() -> bool:
    return _ML_AVAILABLE and _model is not None


def summary() -> dict:
    from backend.src.services.test_signal import test_signal_repo as _tdb
    labeled = len(_tdb.get_ml_training_data())
    return {
        "available":        _ML_AVAILABLE,
        "trained":          is_trained(),
        "labeled_samples":  labeled,
        "min_samples":      MIN_TRAIN_SAMPLES,
        "next_train_in":    max(0, RETRAIN_EVERY - _n_since_last_train) if is_trained()
                            else max(0, MIN_TRAIN_SAMPLES - labeled),
        "regime_trending":  _model_trending is not None,
        "regime_ranging":   _model_ranging  is not None,
        "online_active":    _online_model   is not None,
        "backend":          "LightGBM" if _LGB else "RandomForest",
    }


def ood_distance(features: dict) -> Optional[float]:
    """
    Normalised distance from the training centroid (Mahalanobis-style per-feature).
    Returns None if no retrain has happened yet.
    Values near 0 = familiar; > 2.0 = out-of-distribution signal.
    """
    if _train_centroid is None or not _ML_AVAILABLE:
        return None
    try:
        import numpy as np
        vec      = np.array(to_vector(features), dtype=float)
        centroid = np.array(_train_centroid, dtype=float)
        std      = np.array(_train_std, dtype=float)
        return round(float(np.sqrt(np.mean(((vec - centroid) / std) ** 2))), 3)
    except Exception:
        return None


def _compute_mcc(y_true: list, y_pred: list) -> float:
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    tn = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 0)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return round((tp * tn - fp * fn) / denom, 3) if denom else 0.0


def get_ml_metrics() -> dict:
    """
    Compute R-multiple regression metrics from closed signals that have ml_prob stored.
    ml_prob now stores predicted R-multiple (not win probability).
    """
    from backend.src.services.test_signal import test_signal_repo as _tdb
    data = _tdb.get_ml_monitor_data()

    empty = {
        "n_data": 0,
        "mean_pred_r": None,
        "mean_actual_r": None,
        "accuracy": None,
        "brier_now": None,       # legacy key — kept for UI compat
        "mcc_rolling": None,     # legacy key
        "calibration": [],       # legacy key
        "win_rate_series": [],
        "cumulative_brier": [],  # legacy key
        "rolling_mcc": [],       # legacy key
        "signal_ids": [],
        "train_history": _train_history,
    }
    if not data:
        return empty

    data = sorted(data, key=lambda x: x["signal_id"])
    preds    = [float(d["ml_prob"]) for d in data]
    outcomes = [d["outcome"] for d in data]
    actual_r = [1.0 if o == "win" else (-1.0 if o == "loss" else 0.0) for o in outcomes]

    win_rate_series: list = []
    wins = 0
    for i, o in enumerate(outcomes):
        if o == "win":
            wins += 1
        win_rate_series.append(round(wins / (i + 1) * 100, 1))

    mean_pred_r   = round(sum(preds) / len(preds), 4)
    mean_actual_r = round(sum(actual_r) / len(actual_r), 4)
    correct = sum(1 for p, o in zip(preds, outcomes) if (p >= 0.0) == (o in ("win", "be")))
    accuracy = round(correct / len(preds), 4)

    return {
        "n_data":           len(data),
        "labeled_count":    len(data),
        "mean_pred_r":      mean_pred_r,
        "mean_actual_r":    mean_actual_r,
        "accuracy":         accuracy,
        "brier_now":        None,
        "mcc_rolling":      None,
        "calibration":      [],
        "win_rate_series":  win_rate_series,
        "pred_r_series":    [round(p, 3) for p in preds],
        "actual_r_series":  [round(r, 1) for r in actual_r],
        "cumulative_brier": [],
        "rolling_mcc":      [],
        "signal_ids":       [d["signal_id"] for d in data],
        "train_history":    _train_history,
    }
