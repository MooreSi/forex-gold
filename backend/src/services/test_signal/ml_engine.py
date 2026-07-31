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

FEATURE_NAMES = [
    # -- Momentum / oscillator --
    "rsi_m15",          # RSI(14) of last 15 M15 bars
    "rsi_slope",        # RSI change over last 3 bars
    "rsi_divergence",   # +1 bullish div / -1 bearish / 0 none
    # -- Volatility --
    "atr_ratio",        # current ATR / 20-bar avg ATR
    "ema_gap_atr",      # (EMA20 - EMA50) / ATR on H1
    "price_ema20_atr",  # (price - EMA20) / ATR
    # -- Candle geometry --
    "body_ratio",
    "upper_wick_pct",
    "lower_wick_pct",
    "is_engulfing",
    "is_pin_bar",
    "consec_candles",
    "m15_momentum",     # 5-bar M15 momentum / ATR
    # -- Level context --
    "level_strength",
    "level_dist_atr",
    "nearby_levels_n",
    "level_is_round",
    # -- Signal quality --
    "rr_tp1",
    "sl_dist_atr",
    # -- Direction / context --
    "direction_score",
    "htf_bias_score",
    "bias_alignment",
    "session_score",
    "hour_sin",
    "hour_cos",
    "recent_win_rate",
    # -- Indicators --
    "h4_bias_score",
    "adx_norm",
    "macd_hist_atr",
    "regime_score",
    # -- Volume --
    "vol_ratio",        # current M15 volume / 20-bar avg volume
    "vol_at_level",     # volume on level-touch candle / avg volume
    # -- External market context --
    "dxy_momentum",       # DXY 1-hour return normalised to [-1, +1]
    "us10y_level",        # US 10-year yield (%)
    "vix_level",          # VIX level (normalised: /50)
    "gvz_level",          # CBOE Gold Volatility Index (normalised: /40); >0.5 = elevated gold vol
    "tip_momentum",       # TIP ETF 1h return [-1,+1]; rising = real yields falling = gold tailwind
    # -- Trigger quality (patched at trigger time; creation placeholder = 0.5) --
    "trigger_drift_atr",  # abs(trigger_price - entry_mid) / atr_m15
    # -- MACD momentum alignment --
    "macd_fading",        # +1 = momentum fading in trade direction (good); -1 = accelerating against
    # -- Cross-engine context (injected at signal creation time) --
    "news_proximity_norm",   # 0=imminent news event, 1=safe; 10-min cache
    "equity_drawdown_pct",   # current drawdown from peak [0,1]
    "concurrent_agreement",  # +1 = same-direction signal on bus, -1 = conflicting, 0 = none
]

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


# ── Internal helpers ──────────────────────────────────────────────────────────

def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l < 1e-9:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = v * k + result * (1 - k)
    return result


def _detect_divergence(m15_candles: list[dict], rsi_vals: list[float]) -> float:
    n = min(len(m15_candles), len(rsi_vals), 10)
    if n < 5:
        return 0.0
    lows  = [float(c.get("low",  0) or 0) for c in m15_candles[-n:]]
    highs = [float(c.get("high", 0) or 0) for c in m15_candles[-n:]]
    rsiv  = rsi_vals[-n:]
    mid   = n // 2
    if min(lows[mid:]) < min(lows[:mid]) and min(rsiv[mid:]) > min(rsiv[:mid]) + 2.0:
        return 1.0
    if max(highs[mid:]) > max(highs[:mid]) and max(rsiv[mid:]) < max(rsiv[:mid]) - 2.0:
        return -1.0
    return 0.0


def _count_consecutive(m15_candles: list[dict]) -> int:
    direction = None
    count = 0
    for c in reversed(m15_candles[-10:]):
        cl = float(c.get("close", 0) or 0)
        op = float(c.get("open",  0) or 0)
        d  = 1 if cl > op else -1 if cl < op else 0
        if direction is None:
            direction = d
        if d == direction and d != 0:
            count += d
        else:
            break
    return count


def _get_recent_win_rate(n: int = 5) -> float:
    try:
        from backend.src.services.test_signal import test_signal_repo as _tdb
        recent = _tdb.get_recent_closed_signals(limit=n)
        if not recent:
            return 0.5
        wins = sum(1 for s in recent if s.get("outcome") == "win")
        return wins / len(recent)
    except Exception:
        return 0.5


def _vol_features(m15_candles: list[dict], candidate: dict, atr_m15: float) -> tuple[float, float]:
    """Returns (vol_ratio, vol_at_level)."""
    vols = [float(c.get("volume", 0) or 0) for c in m15_candles]
    if not vols or max(vols) < 1:
        return 1.0, 1.0
    avg_vol = sum(vols[-20:]) / max(len(vols[-20:]), 1)
    if avg_vol < 1:
        return 1.0, 1.0
    # Current candle volume ratio
    vol_ratio = vols[-1] / avg_vol

    # Volume on candle closest to the key level (level touch)
    level_price = float(candidate.get("key_level", 0) or 0)
    if level_price > 0 and atr_m15 > 0:
        best_idx, best_dist = -1, float("inf")
        for i, c in enumerate(m15_candles[-10:], start=len(m15_candles) - 10):
            lo = float(c.get("low",  0) or 0)
            hi = float(c.get("high", 0) or 0)
            dist = max(0.0, max(lo - level_price, level_price - hi))
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx >= 0 and best_dist < atr_m15:
            vol_at_level = vols[best_idx] / avg_vol
        else:
            vol_at_level = 1.0
    else:
        vol_at_level = vol_ratio

    return round(vol_ratio, 4), round(vol_at_level, 4)


# ── Feature extraction ────────────────────────────────────────────────────────

def extract_features(
    m15_candles: list[dict],
    h1_candles: list[dict],
    candidate: dict,
    key_levels: list[dict],
    session: str,
    htf_bias: str,
    atr_m15: float,
    *,
    h4_bias: str = "neutral",
    adx: float = 20.0,
    macd_hist: float = 0.0,
    market_ctx: Optional[dict] = None,
) -> Optional[dict]:
    if len(m15_candles) < 20 or len(h1_candles) < 55 or atr_m15 <= 0:
        return None

    m15_closes = [float(c.get("close", 0) or 0) for c in m15_candles]
    h1_closes  = [float(c.get("close", 0) or 0) for c in h1_candles]

    # RSI
    rsi_series = [_rsi(m15_closes[:i+1]) for i in range(len(m15_closes) - 20, len(m15_closes))]
    rsi_now    = rsi_series[-1] if rsi_series else 50.0
    rsi_slope  = (rsi_series[-1] - rsi_series[-3]) if len(rsi_series) >= 3 else 0.0
    rsi_div    = _detect_divergence(m15_candles, rsi_series)

    # ATR ratio
    atr_vals = []
    for i in range(max(1, len(m15_candles) - 21), len(m15_candles)):
        c, p = m15_candles[i], m15_candles[i - 1]
        hi = float(c.get("high", 0) or 0)
        lo = float(c.get("low",  0) or 0)
        pc = float(p.get("close",0) or 0)
        atr_vals.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    avg_atr   = (sum(atr_vals) / len(atr_vals)) if atr_vals else atr_m15
    atr_ratio = atr_m15 / avg_atr if avg_atr > 0 else 1.0

    # EMA features (H1) — use all available data for proper warmup
    price       = m15_closes[-1]
    ema20_h1    = _ema(h1_closes, 20)
    ema50_h1    = _ema(h1_closes, 50)
    ema_gap_atr = (ema20_h1 - ema50_h1) / atr_m15
    price_ema20 = (price - ema20_h1) / atr_m15

    # Candle geometry
    last_c = m15_candles[-1]
    prev_c = m15_candles[-2]
    c_open  = float(last_c.get("open",  0) or 0)
    c_close = float(last_c.get("close", 0) or 0)
    c_high  = float(last_c.get("high",  0) or 0)
    c_low   = float(last_c.get("low",   0) or 0)
    c_range = max(c_high - c_low, 1e-9)
    body    = abs(c_close - c_open)
    body_ratio     = body / c_range
    upper_wick     = c_high - max(c_open, c_close)
    lower_wick     = min(c_open, c_close) - c_low
    upper_wick_pct = upper_wick / c_range
    lower_wick_pct = lower_wick / c_range

    p_body_lo = min(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    p_body_hi = max(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    p_body    = p_body_hi - p_body_lo
    c_body_lo = min(c_open, c_close)
    c_body_hi = max(c_open, c_close)
    is_engulfing = 1.0 if (c_body_lo <= p_body_lo and c_body_hi >= p_body_hi
                            and body > p_body * 1.05) else 0.0

    max_wick   = max(upper_wick, lower_wick)
    is_pin_bar = 1.0 if (max_wick >= 2.0 * body and body_ratio < 0.35) else 0.0

    consec  = float(_count_consecutive(m15_candles))
    m15_mom = (m15_closes[-1] - m15_closes[-6]) / atr_m15 if len(m15_closes) >= 6 else 0.0

    # Level context
    lv_price    = float(candidate.get("key_level", price))
    lv_strength = float(candidate.get("level_strength", 1))
    lv_dist_atr = abs(lv_price - price) / atr_m15
    nearby_n    = float(sum(1 for lv in key_levels if abs(lv["price"] - price) <= atr_m15))
    lv_is_round = 1.0 if candidate.get("key_level_type") == "round" else 0.0

    # Signal quality
    rr_tp1      = float(candidate.get("rr_tp1", 0))
    sl_dist     = float(candidate.get("sl_dist", atr_m15))
    sl_dist_atr = sl_dist / atr_m15

    # Directional context
    direction       = candidate.get("direction", "BUY")
    direction_score = 1.0 if direction == "BUY" else -1.0
    htf_map         = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
    htf_score       = htf_map.get(htf_bias, 0.0)
    bias_alignment  = direction_score * htf_score
    sess_map        = {"overlap": 1.5, "london": 1.0, "ny": 1.0, "asian": 0.5, "off": 0.0}
    session_score   = sess_map.get(session, 0.0)
    hour            = datetime.now(timezone.utc).hour
    hour_sin        = math.sin(2 * math.pi * hour / 24)
    hour_cos        = math.cos(2 * math.pi * hour / 24)
    recent_wr       = _get_recent_win_rate()

    # Indicators
    h4_score     = htf_map.get(h4_bias, 0.0)
    adx_norm     = min(adx / 50.0, 2.0)
    macd_hist_atr = macd_hist / atr_m15 if atr_m15 > 0 else 0.0
    regime_val   = candidate.get("regime", "neutral")
    regime_score = {"trending": 1.0, "neutral": 0.5, "ranging": 0.0}.get(regime_val, 0.5)

    # Volume
    vol_ratio, vol_at_level = _vol_features(m15_candles, candidate, atr_m15)

    # External context
    ctx          = market_ctx or {}
    dxy_momentum = float(ctx.get("dxy_momentum", 0.0))
    us10y_level  = float(ctx.get("us10y_level",  4.5))
    vix_level    = float(ctx.get("vix_level",    20.0)) / 50.0  # normalise to ~0-1
    gvz_level    = float(ctx.get("gvz_level",    17.0)) / 40.0  # normalise to ~0-1
    tip_momentum = float(ctx.get("tip_momentum",  0.0))

    # MACD momentum alignment: +1 = momentum fading in trade direction (ideal for bounce)
    # -1 = momentum accelerating against the trade (bad for bounce)
    try:
        from backend.src.services.test_signal.signal_generator import compute_macd_hist as _cmh
        _, _hist_now  = _cmh(m15_closes)
        _, _hist_prev = _cmh(m15_closes[:-3]) if len(m15_closes) > 38 else (0.0, _hist_now)
        direction = candidate.get("direction", "BUY")
        if direction == "BUY":
            # Rising MACD = selling fading = good; falling = bad
            _macd_dir = 1.0 if _hist_now > _hist_prev else -1.0
        else:
            # Falling MACD = buying fading = good; rising = bad
            _macd_dir = 1.0 if _hist_now < _hist_prev else -1.0
        macd_fading = _macd_dir
    except Exception:
        macd_fading = 0.0

    return {
        "rsi_m15":         rsi_now,
        "rsi_slope":       rsi_slope,
        "rsi_divergence":  rsi_div,
        "atr_ratio":       atr_ratio,
        "ema_gap_atr":     ema_gap_atr,
        "price_ema20_atr": price_ema20,
        "body_ratio":      body_ratio,
        "upper_wick_pct":  upper_wick_pct,
        "lower_wick_pct":  lower_wick_pct,
        "is_engulfing":    is_engulfing,
        "is_pin_bar":      is_pin_bar,
        "consec_candles":  consec,
        "m15_momentum":    m15_mom,
        "level_strength":  lv_strength,
        "level_dist_atr":  lv_dist_atr,
        "nearby_levels_n": nearby_n,
        "level_is_round":  lv_is_round,
        "rr_tp1":          rr_tp1,
        "sl_dist_atr":     sl_dist_atr,
        "direction_score": direction_score,
        "htf_bias_score":  htf_score,
        "bias_alignment":  bias_alignment,
        "session_score":   session_score,
        "hour_sin":        hour_sin,
        "hour_cos":        hour_cos,
        "recent_win_rate": recent_wr,
        "h4_bias_score":   h4_score,
        "adx_norm":        adx_norm,
        "macd_hist_atr":   macd_hist_atr,
        "regime_score":    regime_score,
        "vol_ratio":       vol_ratio,
        "vol_at_level":    vol_at_level,
        "dxy_momentum":       dxy_momentum,
        "us10y_level":        us10y_level,
        "vix_level":          vix_level,
        "gvz_level":          gvz_level,
        "tip_momentum":       tip_momentum,
        # Placeholder — patched with actual value when signal triggers
        "trigger_drift_atr":  0.5,
        "macd_fading":        macd_fading,
        # Cross-engine context — injected into candidate by engine.py before calling extract_features
        "news_proximity_norm":  float(candidate.get("news_proximity_norm",  1.0)),
        "equity_drawdown_pct":  float(candidate.get("equity_drawdown_pct",  0.0)),
        "concurrent_agreement": float(candidate.get("concurrent_agreement", 0.0)),
    }


def to_vector(features: dict) -> list[float]:
    return [float(features.get(k, 0.0)) for k in FEATURE_NAMES]


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
