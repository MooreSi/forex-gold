"""
Persistent ML signal quality predictor for the BREAKOUT signal engine.

Mirrors the architecture of test_signal/ml_engine.py but uses breakout-specific
features that can be extracted purely from stored bo_signals columns — no M15
candle arrays needed at prediction time.

Feature set (19):
  1. adx_norm              — ADX(14) / 50, clamped 0-1
  2. macd_momentum         — macd_hist / atr_m15 (normalised)
  3. session_score         — overlap 1.5 / london+ny 1.0 / asian 0.5 / off 0.0
  4. direction_score       — BUY=1 / SELL=-1
  5. htf_score             — H1 bias bullish=1 / neutral=0 / bearish=-1
  6. h4_score              — same for H4
  7. bias_alignment        — direction × htf_score
  8. dual_bias             — 1 if H1 and H4 both align with direction
  9. rr_tp1                — risk:reward to TP1
 10. sl_dist_atr           — sl_dist / atr_m15
 11. quality_score         — Claude's quality score (0-1)
 12. bo_type_go            — 1=break-and-go, 0=break-and-retest
 13. hour_sin              — sin(hour/24 × 2π)  (created_at UTC)
 14. hour_cos              — cos(hour/24 × 2π)
 15. recent_win_rate       — win rate of last 10 closed bo_signals
 16. dxy_momentum          — DXY 1h return [-1,+1]; negative = USD weakening = gold tailwind
 17. gvz_level             — CBOE Gold Volatility Index normalised /40
 18. tip_momentum          — TIP ETF 1h return [-1,+1]; rising = real yields falling = gold tailwind
 19. news_proximity_norm   — minutes to next high-impact event / 120, clamped [0,1]; 0=imminent, 1=safe
 20. regime_score          — trending=1.0, ranging=0.0, volatile=0.5 (derived from ADX+ATR)
 21. equity_drawdown_pct   — current drawdown from peak equity [0,1]
 22. concurrent_agreement  — +1 same-dir signal from another engine in last 15min, -1 opposite, 0 none

Architecture:
  - Primary batch model: LightGBM regression (with RandomForestRegressor fallback)
  - Online model: SGDRegressor — updated per-trade between batch retrains
  - Predicts R-multiple (positive = profitable, negative = losing)
  - Walk-forward retraining every RETRAIN_EVERY new labeled samples after MIN_TRAIN_SAMPLES
  - Time-decay weights: recent trades weighted higher (half-life = DECAY_HALFLIFE)
  - Cold start: no prediction until MIN_TRAIN_SAMPLES available
  - Persistent: models saved via joblib; survive restarts
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger("breakout_signal")

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    import numpy as np
    _NP = True
except ImportError:
    _NP = False

try:
    import lightgbm as lgb
    _LGB = True
except ImportError:
    _LGB = False

try:
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    from sklearn.linear_model import SGDClassifier, SGDRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    import joblib
    _SK = True
except ImportError:
    _SK = False

_ML_AVAILABLE = _NP and _SK

# ── Constants ─────────────────────────────────────────────────────────────────
MIN_TRAIN_SAMPLES = 15
RETRAIN_EVERY     = 5
DECAY_HALFLIFE    = 40    # trades (recent trades weighted higher)

FEATURE_NAMES = [
    "adx_norm",
    "macd_momentum",
    "session_score",
    "direction_score",
    "htf_score",
    "h4_score",
    "bias_alignment",
    "dual_bias",
    "rr_tp1",
    "sl_dist_atr",
    "quality_score",
    "bo_type_go",
    "hour_sin",
    "hour_cos",
    "recent_win_rate",
    # -- External market context --
    "dxy_momentum",   # DXY 1h return [-1,+1]; negative = USD weakening = gold tailwind
    "gvz_level",      # CBOE Gold Volatility Index normalised /40
    "tip_momentum",   # TIP ETF 1h return [-1,+1]; rising = real yields falling = gold tailwind
    "news_proximity_norm",  # minutes to next high-impact event / 120, clamped [0,1]; 0=imminent, 1=safe
    "regime_score",         # trending=1.0, ranging=0.0, volatile=0.5 (derived from ADX+ATR)
    "equity_drawdown_pct",  # current drawdown from peak equity [0,1]
    "concurrent_agreement", # +1 same-dir signal from another engine in last 15min, -1 opposite, 0 none
]
N_FEATURES    = len(FEATURE_NAMES)
MODEL_VERSION = N_FEATURES

# Features have only ever been APPENDED, so a stored vector's length identifies
# its generation and the missing tail can be filled with the same neutral
# defaults extract_features() uses when live data is unavailable. This lets
# _retrain() keep the full trade history across feature-version bumps instead
# of discarding every pre-bump sample.
#   v15 → +dxy_momentum 0.0, +gvz_level 17/40, +tip_momentum 0.0
#   v18 → +news_proximity_norm 1.0, +regime_score 0.5,
#          +equity_drawdown_pct 0.0, +concurrent_agreement 0.0
_LEGACY_TAIL = [0.0, 17.0 / 40.0, 0.0, 1.0, 0.5, 0.0, 0.0]  # v15 → v22 fill


def _pad_legacy(feats: list) -> Optional[list]:
    """Upgrade an older, shorter feature vector to N_FEATURES, or None if unknown."""
    if len(feats) == N_FEATURES:
        return feats
    if 15 <= len(feats) < N_FEATURES:
        missing = N_FEATURES - len(feats)
        return list(feats) + _LEGACY_TAIL[-missing:]
    return None

# ── Module-level state ────────────────────────────────────────────────────────
_model:        Optional[object] = None   # batch model (LGB or RF)
_model_online: Optional[object] = None   # SGD online learner
_data_dir:     Optional[Path]   = None
_labeled_count: int = 0
_new_since_retrain: int = 0
_train_history: list = []               # [{ts, n_samples, cv_auc, feature_importances}]


# ── Init ──────────────────────────────────────────────────────────────────────

def init(data_dir: Path) -> None:
    global _data_dir
    _data_dir = Path(data_dir)
    if _ML_AVAILABLE:
        _load_all()
        # Startup backfill: a feature-version bump discards the saved batch model,
        # but the DB history remains trainable via _pad_legacy(). Rebuild it now
        # rather than waiting for the labeled-count meta to reach MIN_TRAIN_SAMPLES
        # again (~15 fresh trades with the live gate running on a noisy online SGD).
        if _model is None:
            try:
                _retrain()
                if _model is not None:
                    _save_all()
                    _log.info("[BO-ML] batch model rebuilt from padded legacy history")
            except Exception as e:
                _log.warning("[BO-ML] startup retrain failed: %s", e)
        _log.info(
            "[BO-ML] init — labeled=%d  trained=%s  lgb=%s",
            _labeled_count, is_trained(), _LGB,
        )
    else:
        _log.warning("[BO-ML] ML dependencies not available (numpy/sklearn missing)")


# ── Feature extraction ────────────────────────────────────────────────────────

def _bias_score(bias: str) -> float:
    b = (bias or "neutral").lower()
    if "bullish" in b:
        return 1.0
    if "bearish" in b:
        return -1.0
    return 0.0


def _session_score(session: str) -> float:
    s = (session or "off").lower()
    if "overlap" in s:
        return 1.5
    if "london" in s or "ny" in s or "new_york" in s:
        return 1.0
    if "asian" in s or "asia" in s:
        return 0.5
    return 0.0


def extract_features(signal_data: dict, market_ctx: Optional[dict] = None) -> Optional[list[float]]:
    """
    Extract ML features from a signal dict (bo_signals row or sig_data at creation).
    market_ctx: optional dict from market_context.get_context() — provides macro features.
    Returns None if essential fields are missing or ATR is zero.
    """
    try:
        atr = float(signal_data.get("atr_m15") or 0)
        if atr < 0.01:
            return None

        adx       = float(signal_data.get("adx_at_signal") or 0)
        macd_hist = float(signal_data.get("macd_hist") or 0)
        session   = signal_data.get("session") or "off"
        direction = (signal_data.get("direction") or "").upper()
        htf_bias  = signal_data.get("htf_bias") or "neutral"
        h4_bias   = signal_data.get("h4_bias")  or "neutral"
        rr_tp1    = float(signal_data.get("rr_tp1") or 0)
        sl_dist   = float(signal_data.get("sl_dist") or 0)
        quality   = float(signal_data.get("quality_score") or 0)
        bo_type   = (signal_data.get("breakout_type") or "go").lower()
        created   = float(signal_data.get("created_at") or time.time())

        dir_score = 1.0 if direction == "BUY" else -1.0
        htf_score = _bias_score(htf_bias)
        h4_score  = _bias_score(h4_bias)

        dual_bias = 1.0 if (
            (direction == "BUY"  and htf_score > 0 and h4_score > 0) or
            (direction == "SELL" and htf_score < 0 and h4_score < 0)
        ) else 0.0

        dt   = datetime.fromtimestamp(created, tz=timezone.utc)
        hour = dt.hour + dt.minute / 60.0
        angle = hour / 24.0 * 2 * math.pi

        try:
            from backend.src.services.breakout_signal.breakout_signal_repo import get_recent_win_rate
            recent_wr = get_recent_win_rate(n=10)
        except Exception:
            recent_wr = 0.5

        # Macro context features — use stored signal fields when present (retraining
        # from DB rows), else pull from the live context dict passed at creation time.
        ctx = market_ctx or {}
        dxy_momentum = float(signal_data.get("dxy_momentum") or ctx.get("dxy_momentum") or 0.0)
        gvz_level    = float(signal_data.get("gvz_level")    or ctx.get("gvz_level")    or 17.0) / 40.0
        tip_momentum = float(signal_data.get("tip_momentum") or ctx.get("tip_momentum") or 0.0)

        # New features — pulled from signal_data dict or ctx, defaulting to neutral values
        news_proximity = float(signal_data.get("news_proximity_norm") or ctx.get("news_proximity_norm") or 1.0)
        regime_score   = float(signal_data.get("regime_score")        or ctx.get("regime_score")        or 0.5)
        eq_dd          = float(signal_data.get("equity_drawdown_pct") or ctx.get("equity_drawdown_pct") or 0.0)
        concurrent     = float(signal_data.get("concurrent_agreement")or ctx.get("concurrent_agreement") or 0.0)

        return [
            min(1.0, adx / 50.0),                      # adx_norm
            max(-3.0, min(3.0, macd_hist / atr)),       # macd_momentum
            _session_score(session),                     # session_score
            dir_score,                                   # direction_score
            htf_score,                                   # htf_score
            h4_score,                                    # h4_score
            dir_score * htf_score,                       # bias_alignment
            dual_bias,                                   # dual_bias
            min(5.0, max(0.0, rr_tp1)),                 # rr_tp1
            min(5.0, sl_dist / atr if atr > 0 else 0),  # sl_dist_atr
            min(1.0, max(0.0, quality)),                 # quality_score
            1.0 if bo_type == "go" else 0.0,            # bo_type_go
            math.sin(angle),                             # hour_sin
            math.cos(angle),                             # hour_cos
            recent_wr,                                   # recent_win_rate
            dxy_momentum,                                # dxy_momentum
            gvz_level,                                   # gvz_level
            tip_momentum,                                # tip_momentum
            news_proximity,                              # news_proximity_norm
            regime_score,                                # regime_score
            eq_dd,                                       # equity_drawdown_pct
            concurrent,                                  # concurrent_agreement
        ]

    except Exception as e:
        _log.debug("[BO-ML] extract_features error: %s", e)
        return None


# ── Prediction ────────────────────────────────────────────────────────────────

def predict(features: list[float]) -> Optional[float]:
    """Return predicted R-multiple or None if model not yet trained.
    Positive = expected profitable, negative = expected losing.
    Blends batch model (60%) + online SGD (40%) when both available."""
    if not _ML_AVAILABLE or not is_trained():
        return None
    if not features or len(features) != N_FEATURES:
        return None
    try:
        X = np.array(features, dtype=float).reshape(1, -1)
        preds = []

        if _model is not None:
            if _LGB and hasattr(_model, "predict"):
                p = float(_model.predict(X)[0])
            else:
                p = float(_model.predict(X)[0])
            preds.append((p, 0.6))

        if _model_online is not None:
            p_online = float(_model_online.predict(X)[0])
            preds.append((p_online, 0.4))

        if not preds:
            return None
        total_w = sum(w for _, w in preds)
        blended = sum(p * w for p, w in preds) / total_w
        return round(float(blended), 4)

    except Exception as e:
        _log.debug("[BO-ML] predict error: %s", e)
        return None


# ── Outcome recording ─────────────────────────────────────────────────────────

def record_outcome(signal_id: int, outcome: str) -> None:
    """
    Called when a signal closes. Updates online model and triggers batch retrain
    when enough new labeled samples have accumulated.
    """
    if not _ML_AVAILABLE:
        return
    global _labeled_count, _new_since_retrain

    from backend.src.services.breakout_signal import breakout_signal_repo as bdb

    features_json = bdb.get_ml_features_for_signal(signal_id)
    if not features_json:
        _log.debug("[BO-ML] No features for signal %d — skipping", signal_id)
        return

    try:
        feats = json.loads(features_json) if isinstance(features_json, str) else features_json
    except Exception:
        return

    feats = _pad_legacy(feats) if feats else None
    if not feats:
        return

    rr_tp1 = 1.0
    try:
        sig_row = bdb.get_signal_by_id(signal_id)
        rr_tp1 = float((sig_row or {}).get("rr_tp1") or 1.0)
    except Exception:
        pass
    label = rr_tp1 if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)
    _labeled_count    += 1
    _new_since_retrain += 1

    _online_update(feats, label)

    if _labeled_count >= MIN_TRAIN_SAMPLES and _new_since_retrain >= RETRAIN_EVERY:
        _retrain()
        _new_since_retrain = 0

    _save_all()


# ── Online update ─────────────────────────────────────────────────────────────

def _online_update(features: list[float], label: float) -> None:
    global _model_online
    if not _ML_AVAILABLE:
        return
    try:
        X = np.array(features, dtype=float).reshape(1, -1)

        if _model_online is None:
            _model_online = Pipeline([
                ("scaler", StandardScaler()),
                ("reg",    SGDRegressor(loss="huber", epsilon=0.1, max_iter=1,
                                        warm_start=True, random_state=42)),
            ])
        sw = np.array([2.0 if label < 0 else 1.0])  # weight losses higher
        _model_online.named_steps["scaler"].partial_fit(X)
        X_s = _model_online.named_steps["scaler"].transform(X)
        _model_online.named_steps["reg"].partial_fit(X_s, np.array([label]), sample_weight=sw)

    except Exception as e:
        _log.debug("[BO-ML] online_update error: %s", e)


# ── Batch retrain ─────────────────────────────────────────────────────────────

def _retrain() -> None:
    global _model, _labeled_count
    if not _ML_AVAILABLE:
        return

    from backend.src.services.breakout_signal import breakout_signal_repo as bdb
    rows = bdb.get_ml_training_data()
    if not rows:
        return

    X_list, y_list, w_list = [], [], []
    n = len(rows)
    for i, row in enumerate(rows):
        try:
            raw = row.get("ml_features_json") or row.get("features_json")
            feats = json.loads(raw) if isinstance(raw, str) else raw
            feats = _pad_legacy(feats) if feats else None
            if not feats:
                continue
            # R-multiple: win uses rr_tp1, loss = -1.0, be = 0.0
            _outcome = row.get("outcome", "loss")
            _rr = float(row.get("rr_tp1") or 1.0)
            label = _rr if _outcome == "win" else (-1.0 if _outcome == "loss" else 0.0)
            weight = math.exp(math.log(0.5) / DECAY_HALFLIFE * (n - 1 - i))
            X_list.append(feats)
            y_list.append(label)
            w_list.append(weight)
        except Exception:
            continue

    if len(X_list) < MIN_TRAIN_SAMPLES:
        _log.debug("[BO-ML] Not enough labeled samples yet (%d/%d)", len(X_list), MIN_TRAIN_SAMPLES)
        return

    X = np.array(X_list, dtype=float)
    y = np.array(y_list, dtype=float)
    w = np.array(w_list, dtype=float)

    _labeled_count = len(X_list)

    fi: dict = {}
    cv_auc: float = 0.5
    _lgb_ok = False

    if _LGB:
        try:
            import lightgbm as lgb
            ds = lgb.Dataset(X, label=y, weight=w)
            params = {
                "objective":        "regression",
                "metric":           "l2",
                "num_leaves":       15,
                "learning_rate":    0.05,
                "feature_fraction": 0.8,
                "verbose":          -1,
                "min_data_in_leaf": 3,
                "n_estimators":     100,
            }
            new_model = lgb.train(params, ds, num_boost_round=100)
            _model = new_model
            raw_fi = new_model.feature_importance(importance_type="gain")
            total  = max(raw_fi.sum(), 1e-9)
            fi = {FEATURE_NAMES[i]: round(float(raw_fi[i] / total), 4)
                  for i in range(len(FEATURE_NAMES))}
            _lgb_ok = True
            _log.info("[BO-ML] LightGBM retrained on %d samples", len(X_list))
        except Exception as e:
            _log.warning("[BO-ML] LightGBM retrain error: %s — falling back to RF", e)

    if not _lgb_ok:
        try:
            rf = RandomForestRegressor(
                n_estimators=80,
                max_depth=5,
                random_state=42,
            )
            rf.fit(X, y, sample_weight=w)
            _model = rf
            raw_fi = rf.feature_importances_
            total  = max(raw_fi.sum(), 1e-9)
            fi = {FEATURE_NAMES[i]: round(float(raw_fi[i] / total), 4)
                  for i in range(len(FEATURE_NAMES))}
            _log.info("[BO-ML] RandomForestRegressor retrained on %d samples", len(X_list))
        except Exception as e:
            _log.warning("[BO-ML] RF retrain error: %s", e)

    import time as _t
    _train_history.append({
        "ts":                  _t.time(),
        "n_samples":           len(X_list),
        "cv_auc":              cv_auc,
        "feature_importances": fi,
    })


# ── Persistence ───────────────────────────────────────────────────────────────

def _model_path(name: str) -> Optional[Path]:
    if _data_dir is None:
        return None
    return _data_dir / f"bo_ml_{name}_v{MODEL_VERSION}.joblib"


def _save_all() -> None:
    if not _ML_AVAILABLE or _data_dir is None:
        return
    try:
        meta = {"labeled_count": _labeled_count, "version": MODEL_VERSION}
        joblib.dump(meta, _model_path("meta"))
        if _model is not None:
            joblib.dump(_model, _model_path("batch"))
        if _model_online is not None:
            joblib.dump(_model_online, _model_path("online"))
    except Exception as e:
        _log.debug("[BO-ML] save error: %s", e)


def _load_all() -> None:
    global _model, _model_online, _labeled_count
    if not _ML_AVAILABLE or _data_dir is None:
        return
    try:
        meta_path = _model_path("meta")
        if meta_path and meta_path.exists():
            meta = joblib.load(meta_path)
            if meta.get("version") != MODEL_VERSION:
                _log.info("[BO-ML] Model version mismatch — starting fresh")
                return
            _labeled_count = meta.get("labeled_count", 0)
    except Exception:
        return

    try:
        p = _model_path("batch")
        if p and p.exists():
            _model = joblib.load(p)
    except Exception as e:
        _log.debug("[BO-ML] load batch error: %s", e)

    try:
        p = _model_path("online")
        if p and p.exists():
            loaded = joblib.load(p)
            # Verify it's a regressor pipeline (not old classifier)
            reg = loaded.named_steps.get("reg") if hasattr(loaded, "named_steps") else None
            if reg is not None and hasattr(reg, "t_"):
                _model_online = loaded
            else:
                _log.info("[BO-ML] Online model is old classifier format — resetting to regressor")
    except Exception as e:
        _log.debug("[BO-ML] load online error: %s", e)


# ── Status helpers ────────────────────────────────────────────────────────────

def is_trained() -> bool:
    return _model is not None or _model_online is not None


def has_batch() -> bool:
    """True only when the batch model exists — the live-execution gate and
    lot-scaling must not act on the online SGD alone (see engine._execute_live)."""
    return _model is not None


def summary() -> dict:
    return {
        "trained":       is_trained(),
        "labeled_count": _labeled_count,
        "min_needed":    MIN_TRAIN_SAMPLES,
        "has_batch":     _model is not None,
        "has_online":    _model_online is not None,
        "lgb_available": _LGB,
        "ml_available":  _ML_AVAILABLE,
        "features":      FEATURE_NAMES,
        "n_features":    N_FEATURES,
    }


def get_ml_metrics() -> dict:
    """Regression learning metrics: mean predicted R, mean actual R, directional accuracy."""
    _blank = {
        "brier_now":        None,
        "mcc_rolling":      None,
        "n_data":           0,
        "calibration":      [],
        "win_rate_series":  [],
        "pred_r_series":    [],
        "actual_r_series":  [],
        "cumulative_brier": [],
        "rolling_mcc":      [],
        "signal_ids":       [],
        "mean_pred_r":      None,
        "mean_actual_r":    None,
        "accuracy":         None,
        "train_history":    _train_history,
        "labeled_count":    _labeled_count,
    }
    if not _ML_AVAILABLE:
        return _blank
    try:
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        rows = bdb.fetch_ml_outcome_rows()
        if not rows:
            return {**_blank, "train_history": _train_history}

        signal_ids, pred_rs, actual_rs = [], [], []
        actuals_win: list = []
        for row in rows:
            outcome = (row[3] or "").lower()
            if outcome not in ("win", "loss", "be"):
                continue
            rr_tp1 = float(row[4] or 1.0)
            actual_r = rr_tp1 if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)
            signal_ids.append(row[1] or str(row[0]))
            pred_rs.append(float(row[2]))
            actual_rs.append(actual_r)
            actuals_win.append(1 if outcome == "win" else 0)

        if not signal_ids:
            return {**_blank, "train_history": _train_history}

        n = len(signal_ids)
        mean_pred_r   = round(sum(pred_rs) / n, 4)
        mean_actual_r = round(sum(actual_rs) / n, 4)

        # Directional accuracy: sign(predicted) == sign(actual)
        correct = sum(
            1 for p, a in zip(pred_rs, actual_rs)
            if (p >= 0) == (a >= 0)
        )
        accuracy = round(correct / n, 4) if n > 0 else None

        # Cumulative win rate series (for UI charts)
        win_rate_series: list = []
        wins = 0
        for i, a in enumerate(actuals_win):
            wins += a
            win_rate_series.append(round(wins / (i + 1) * 100, 1))

        return {
            "brier_now":        None,
            "mcc_rolling":      None,
            "n_data":           n,
            "calibration":      [],
            "win_rate_series":  win_rate_series,
            "pred_r_series":    [round(p, 3) for p in pred_rs],
            "actual_r_series":  [round(r, 1) for r in actual_rs],
            "cumulative_brier": [],
            "rolling_mcc":      [],
            "signal_ids":       signal_ids,
            "mean_pred_r":      mean_pred_r,
            "mean_actual_r":    mean_actual_r,
            "accuracy":         accuracy,
            "train_history":    _train_history,
            "labeled_count":    _labeled_count,
        }
    except Exception as e:
        _log.debug("[BO-ML] metrics error: %s", e)
        return {**_blank, "train_history": _train_history}
