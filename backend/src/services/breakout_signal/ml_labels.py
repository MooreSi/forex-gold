"""What the Breakout model learns, how each row is weighted, and when its
opinion counts. docs/todo/017 items 1 and 3.

**The label.** Until now `rr_tp1` for a win, -1.0 for a loss, 0 for a
breakeven. On the owner's Mac (2026-10-14, 157 closed rows) `rr_tp1` is 1.0 on
every row, so the model learns +1 / -1: wins really realised +1.02R and
losses -0.84R, and the spread inside each class is thrown away. With
`bo_ml_realised_r_label` on it learns realised R, the Reversal engine's v5
definition: net dollars over the dollars the signal's own stop put at risk
(`sl_dist` x `lot_size` x 100), clamped. Executed rows are left out of it, as
Reversal's are: their dollars are the broker's, at a lot and stop this row
does not record. None exist yet on that machine.

**The arming rule** (Trend PA's, made stricter): under the new label the
model gives no opinion until it has at least `ARM_MIN_ROWS` labelled rows and
ranks the newest `ARM_HOLDOUT` of them at AUC >= `ARM_MIN_AUC`, significantly
above 0.5, when fitted on the rest.
No opinion means the gate lets the signal through, so this can block fewer
signals than today, not more.

**The weights** (`bo_ml_time_decay_enabled`): `market/sample_weights`, by age
in days with a floor and discounted for overlap, instead of the 40-trade
half-life.

Pure apart from `switches()`, which reads two settings. Nothing here places,
closes or modifies a trade.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Optional, Sequence

LEGACY = "rr_tp1"
REALISED = "realised_r"

R_CLAMP = 12.0
DOLLARS_PER_POINT_PER_LOT = 100.0
DEFAULT_LOT = 0.10

ARM_MIN_ROWS = 60
ARM_MIN_AUC = 0.55
ARM_HOLDOUT = 0.25
# AUC alone is not enough on a holdout this small: on 40 pure-noise histories
# of 120 rows (30 in the holdout), 6 cleared 0.55 (2026-10-14). The
# holdout AUC must also beat 0.5 at one-sided 5% (Mann-Whitney, normal
# approximation).
ARM_MAX_P_Z = 1.645

# Provisional, as for Reversal: what "on" means until the offline replay says
# whether any setting beats today's. docs/todo/017.
DECAY_HALF_LIFE_DAYS = 14.0
DECAY_FLOOR = 0.25

_SWITCH_TTL_S = 30.0
_cache: dict = {"at": 0.0, "value": (False, False)}


def switches(now: Optional[float] = None) -> tuple[bool, bool]:
    """(bo_ml_time_decay_enabled, bo_ml_realised_r_label), cached 30 s.
    Off on any failure."""
    now = time.time() if now is None else now
    if now - _cache["at"] < _SWITCH_TTL_S:
        return _cache["value"]
    try:
        from backend.src.db import database as _db
        from backend.src.services.risk import capability_gates as _caps
        rs = _db.get_risk_settings() or {}
        value = (bool(_caps.bo_ml_time_decay_enabled(rs)),
                 bool(_caps.bo_ml_realised_r_label(rs)))
    except Exception:
        value = (False, False)
    _cache.update(at=now, value=value)
    return value


def mode() -> str:
    return REALISED if switches()[1] else LEGACY


def legacy_label(outcome: str, rr_tp1) -> float:
    """The label as it has always been."""
    try:
        rr = float(rr_tp1 or 1.0)
    except (TypeError, ValueError):
        rr = 1.0
    return rr if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)


def realised_r(row: dict) -> Optional[float]:
    """Net dollars over the signal's own risk, or None when the row can't say."""
    if row.get("live_exec_status") in ("executed", "success") or row.get("mt5_ticket"):
        return None
    try:
        lot = float(row.get("lot_size") or DEFAULT_LOT)
        risk = float(row.get("sl_dist") or 0.0) * lot * DOLLARS_PER_POINT_PER_LOT
        net = row.get("net_pnl_dollars")
        if net is None:
            net = row.get("pnl_dollars")
        if net is None or risk <= 0 or not math.isfinite(risk):
            return None
        value = float(net) / risk
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(-R_CLAMP, min(R_CLAMP, value))


def row_label(row: dict, label_mode: str) -> Optional[float]:
    if label_mode == REALISED:
        return realised_r(row)
    return legacy_label(row.get("outcome", "loss"), row.get("rr_tp1"))


_OUTCOME_COLUMNS = ("id", "signal_ref", "ml_prob", "outcome", "rr_tp1", "sl_dist",
                    "lot_size", "net_pnl_dollars", "pnl_dollars", "live_exec_status")


def outcome_row(row) -> dict:
    """A `fetch_ml_outcome_rows` row (positional) as a dict."""
    return dict(zip(_OUTCOME_COLUMNS, tuple(row)))


def effective_n(weights) -> float:
    from backend.src.services.market import sample_weights as _sw
    return round(_sw.effective_size(list(weights)), 1)


def learning_description(label_mode: str, gate_armed: bool, history: list) -> dict:
    """What the model learns from, for the "Is it learning?" panel."""
    last = history[-1] if history else {}
    decay_on = switches()[0]
    return {
        "label": "realised R" if label_mode == REALISED else "rr_tp1 / -1 / 0 (the old label)",
        "weights": (f"by age: half-life {DECAY_HALF_LIFE_DAYS:g} days, floor "
                    f"{DECAY_FLOOR:g}, overlap discounted") if decay_on
        else "positional (40-trade half-life)",
        "armed": bool(gate_armed),
        "holdout_auc": last.get("cv_auc") if label_mode == REALISED else None,
        "effective_n": last.get("effective_n"),
    }


def online_weight(label: float, label_mode: str) -> float:
    """The online learner's 2x on losses belongs to the +1 / -1 label only."""
    if label_mode == REALISED:
        return 1.0
    return 2.0 if label < 0 else 1.0


def span(row: dict) -> tuple[float, float]:
    start = float(row.get("trigger_time") or row.get("created_at") or 0.0)
    end = float(row.get("close_time") or start)
    return start, max(start, end)


def time_weights(spans: Sequence[tuple[float, float]]) -> list[float]:
    from backend.src.services.market import sample_weights as _sw
    return _sw.training_weights(spans, DECAY_HALF_LIFE_DAYS, DECAY_FLOOR)


def auc(y_true: Sequence[int], scores: Sequence[float]) -> Optional[float]:
    """Rank AUC with ties averaged; None with only one class."""
    pairs = sorted(zip(scores, y_true))
    pos = sum(1 for _, t in pairs if t)
    neg = len(pairs) - pos
    if not pos or not neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        rank_sum += avg * sum(1 for k in range(i, j + 1) if pairs[k][1])
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def auc_z(a: float, n_pos: int, n_neg: int) -> float:
    """How many standard errors `a` sits above 0.5 under no skill."""
    if n_pos <= 0 or n_neg <= 0:
        return 0.0
    se = math.sqrt((n_pos + n_neg + 1) / (12.0 * n_pos * n_neg))
    return (a - 0.5) / se


def holdout_auc(X: list, y: list, w: list,
                fit: Callable[[list, list, list], object]) -> Optional[tuple]:
    """Fit on the oldest rows, score the newest `ARM_HOLDOUT`, by time:
    (auc, z), or None below `ARM_MIN_ROWS` or with one class.
    Rows must already be oldest first."""
    n = len(X)
    if n < ARM_MIN_ROWS:
        return None
    cut = int(round(n * (1.0 - ARM_HOLDOUT)))
    if cut < 1 or cut >= n:
        return None
    model = fit(X[:cut], y[:cut], w[:cut])
    if model is None:
        return None
    import numpy as np
    preds = model.predict(np.array(X[cut:], dtype=float))
    truth = [1 if v > 0 else 0 for v in y[cut:]]
    a = auc(truth, [float(p) for p in preds])
    if a is None:
        return None
    return a, auc_z(a, sum(truth), len(truth) - sum(truth))


def armed(holdout: Optional[tuple]) -> bool:
    return (holdout is not None and holdout[0] >= ARM_MIN_AUC
            and holdout[1] >= ARM_MAX_P_Z)
