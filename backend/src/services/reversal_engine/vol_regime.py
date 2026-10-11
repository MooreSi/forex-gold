"""How volatile the market is compared with its own recent history.

docs/todo/016. One definition, used by two consumers so they cannot drift:

  * the Reversal ML model's optional `vol_regime` feature, behind
    `re_vol_regime_feature_enabled` (off by default), and
  * volatility-target sizing, behind `vol_target_sizing_enabled` (off by
    default), which needs the same current-vs-reference ATR pair.

**Reference ATR** is the median of the engine's own logged M15 ATR over the
five days before the moment in question (`re_analysis_log`, one row per
cycle), with at least `MIN_SAMPLES` readings. The same function scores a live
signal and a stored one, so training and prediction agree on what "normal"
was at the time.

**Why the feature is not in the stored vector.** Appending it to
`FEATURE_NAMES` would change the model's width with the switch OFF, and a
width change replaces the online SGD model at the next retrain, which moves
every prediction. So the stored vector is untouched, the ratio lives in its
own column (`re_signals.atr_ratio`), and it joins the vector only when the
switch is on (`live_tail`, `row_tail`, `fit_tail`). Off, every list these
return is empty and the model sees exactly what it saw before.

**The training history is reused, not dropped.** `backfill` measures every
stored signal against the log, so the moment the switch is turned on, the
next retrain has the feature for the whole history (7,553 rows on the
owner's Mac on 2026-10-11, about 99% of them with a reference). It runs every
minute from the research loop whether the switch is on or not; a measured row
is never re-read.

Nothing here places, closes or modifies an order.
"""
from __future__ import annotations

import bisect
import logging
import math
import statistics
import time
from typing import Optional

_log = logging.getLogger(__name__)

WINDOW_S = 5 * 86400
MIN_SAMPLES = 200
# log(ratio) clamp: a ratio of e**1.5, about 4.5x normal, is as far as it goes.
CLAMP = 1.5
# The neutral is 0.0 (= normal volatility, log 1). Zero also means "no
# reference": a row the log cannot measure carries no information.
NEUTRAL = 0.0
# A latest ATR older than this is not "current" for sizing.
MAX_ATR_AGE_S = 15 * 60

_REF_TTL_S = 600.0
_SWITCH_TTL_S = 30.0
_ref_cache: dict = {"at": 0.0, "value": None}
_switch_cache: dict = {"at": 0.0, "value": False}


# ── Pure ─────────────────────────────────────────────────────────────────────

def reference_from(atrs: list[float]) -> Optional[float]:
    """Median of `atrs`, or None when there are too few to call it normal."""
    vals = [a for a in atrs if a and a > 0]
    if len(vals) < MIN_SAMPLES:
        return None
    return float(statistics.median(vals))


def ratio(atr: Optional[float], reference: Optional[float]) -> Optional[float]:
    try:
        a, r = float(atr or 0.0), float(reference or 0.0)
    except (TypeError, ValueError):
        return None
    if a <= 0 or r <= 0 or not math.isfinite(a / r):
        return None
    return a / r


def feature(atr_ratio: Optional[float]) -> float:
    """log(ratio), clamped; NEUTRAL when the ratio is missing or unusable."""
    try:
        x = float(atr_ratio or 0.0)
    except (TypeError, ValueError):
        return NEUTRAL
    if x <= 0 or not math.isfinite(x):
        return NEUTRAL
    return round(max(-CLAMP, min(CLAMP, math.log(x))), 6)


def fit_tail(features: list, tail: list, model_width: Optional[int],
             base_width: int) -> list:
    """The vector to score, given the model actually installed.

    The model only learns the tail at its next retrain (every 5 closes), so
    for a few minutes after the switch moves the installed model is the other
    width. Switched on, a base-width model scores the base vector exactly as
    before. Switched off, a model fitted with the tail gets the neutral in
    that slot rather than raising, which would fail the gate open.
    """
    if tail and model_width == base_width:
        return features
    if not tail and model_width == base_width + 1:
        return features + [NEUTRAL]
    return features + tail


# ── Reading the switch and the log ───────────────────────────────────────────

def feature_enabled(now: Optional[float] = None) -> bool:
    """`re_vol_regime_feature_enabled`, cached for 30s. Any failure is OFF."""
    now = time.time() if now is None else now
    if now - _switch_cache["at"] < _SWITCH_TTL_S:
        return bool(_switch_cache["value"])
    try:
        from backend.src.db import database as _db
        from backend.src.services.risk import capability_gates as _caps
        value = _caps.vol_regime_feature_enabled(_db.get_risk_settings() or {})
    except Exception:
        value = False
    _switch_cache.update(at=now, value=bool(value))
    return bool(value)


def reference_atr(at_ts: float) -> Optional[float]:
    """Reference ATR for the five days before `at_ts`. Uncached."""
    from backend.src.services.reversal_engine import vol_regime_repo as _repo
    try:
        series = _repo.atr_series(at_ts - WINDOW_S, at_ts)
    except Exception:
        return None
    return reference_from([a for _, a in series])


def reference_atr_now(now: Optional[float] = None) -> Optional[float]:
    """`reference_atr(now)`, cached for ten minutes: a five-day median does
    not move in ten minutes, and this is read on every scored signal."""
    now = time.time() if now is None else now
    if now - _ref_cache["at"] < _REF_TTL_S:
        return _ref_cache["value"]
    value = reference_atr(now)
    _ref_cache.update(at=now, value=value)
    return value


def latest_atr(now: Optional[float] = None) -> Optional[float]:
    """The engine's most recent logged ATR, if it is under 15 minutes old."""
    now = time.time() if now is None else now
    from backend.src.services.reversal_engine import vol_regime_repo as _repo
    try:
        return _repo.latest_atr(now - MAX_ATR_AGE_S)
    except Exception:
        return None


# ── The ML tail ──────────────────────────────────────────────────────────────

def live_tail(signal_data: Optional[dict]) -> list:
    """[feature] for a signal being scored now, or [] with the switch off."""
    if not signal_data or not feature_enabled():
        return []
    return [feature(ratio(signal_data.get("atr"), reference_atr_now()))]


def row_tail(row: dict, enabled: bool) -> list:
    """[feature] for a stored signal, or [] when `enabled` is False.

    `enabled` is passed in so one training pass reads the switch once, and
    every row in it has the same width.
    """
    if not enabled:
        return []
    return [feature(row.get("atr_ratio"))]


# ── Backfill ─────────────────────────────────────────────────────────────────

def backfill(limit: int = 2000) -> int:
    """Measure up to `limit` signals whose `atr_ratio` is still NULL, oldest
    first. Returns how many. The first pass on an existing install works
    through the history 2,000 rows a minute rather than in one long write.

    One read of the log covers the whole batch: the series is loaded once
    from the earliest window start and each row's window is a bisected
    slice of it. A row the log cannot measure is stored as 0.0, which
    `feature` reads as the neutral, so it is not selected again.
    """
    from backend.src.services.reversal_engine import vol_regime_repo as _repo
    rows = _repo.signals_missing_ratio(limit)
    if not rows:
        return 0
    first = min(float(r["created_at"]) for r in rows)
    last = max(float(r["created_at"]) for r in rows)
    series = _repo.atr_series(first - WINDOW_S, last)
    stamps = [t for t, _ in series]
    atrs = [a for _, a in series]
    out: list[tuple[int, float]] = []
    for r in rows:
        at = float(r["created_at"])
        lo = bisect.bisect_left(stamps, at - WINDOW_S)
        hi = bisect.bisect_left(stamps, at)
        ref = reference_from(atrs[lo:hi])
        out.append((int(r["id"]), ratio(r.get("atr"), ref) or 0.0))
    _repo.store_ratios(out)
    return len(out)


async def vol_ratio_sweep(_engine=None) -> None:
    """The research loop's per-minute pass. Data only: writes a column."""
    from backend.src.db import database as _db
    n = await _db.to_db_thread(backfill)
    if n:
        _log.info("[RE-VolRegime] measured atr_ratio for %d signals", n)
