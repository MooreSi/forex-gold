"""A model that must prove it has an edge before it may pass a trade.

docs/todo/reversal-engine/240. Read by the live path only when
`re_require_proven_edge` is on (`capability_gates.require_proven_edge`,
migration 53, off by default). With it off, nothing here changes a trade.

What the production `ml_engine` (v9) does that this does not:

  * **Its label is a trade nobody places.** v9 regresses the engine's own
    virtual R, where a loss's fill sits ~6.8 points from its stop and TP1
    ~2.1 away. The EA template trades a 5-point stop and a 4-point first
    target. This learns `re_signals.tpl_r`, the template's own exits
    replayed from the real trigger (`tpl_label`).
  * **It refits every 5 closes with no holdout**, and blends in an online
    SGD model that moves the score after every single close. This refits
    once a London day, with no online half.
  * **It trees down to 5 signals a leaf.** This needs 100.
  * **It reads features measured as noise**: see DROPPED.
  * **It fails open**: no model, no block. This fails CLOSED. It passes a
    trade only when it is proven -- the trades it would have taken,
    predicted walk-forward on history it had not seen, made money after
    costs -- and it predicts a non-negative R for this one.

Measured on 2026-09-24 on the engine's whole history, no model, feature or
entry rule cleared that bar (240's Phase 1). So the expected behaviour of
the switch today is that the engine places no orders. That is the point:
it trades again when the evidence says it should, not before.

Nothing here places, closes or modifies a trade.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from backend.src.services.reversal_engine.ml_engine._feature_schema import (
    FEATURE_NAMES, pad_to_schema)

log = logging.getLogger("reversal_engine")

# Features the 2026-09-24 analysis found to be noise or bookkeeping rather
# than facts about the market at the signal:
#   recent_win_rate      the last 20 outcomes: autocorrelated, not causal
#   ref_level_win_rate   a running tally the model's own history feeds
#   ref_*_score          one AI-derived constant per day for every signal
#   level_score          its 0.9 band is the biggest losing band on record
#   pro_likeness         another model's output; ranks nothing here
#   equity_drawdown_pct  the account's state, not the market's
DROPPED = frozenset({
    "recent_win_rate", "ref_level_win_rate", "ref_discipline_score",
    "ref_aggression_score", "level_score", "pro_likeness",
    "equity_drawdown_pct",
})
KEPT = [i for i, n in enumerate(FEATURE_NAMES) if n not in DROPPED]

MIN_ROWS = 1000
MIN_ACCEPTED = 200
MIN_CLUSTERS = 30
MIN_T = 3.0
FOLDS = 5
# The last 60% of history is predicted; the first 40% is only ever trained on.
FIRST_TEST_FRAC = 0.4
# A label spans up to the replay horizon (6h) after its trigger, and a
# signal can wait 2h before triggering. Training rows must end this long
# before the first test row, or the fold is scored on trades whose outcome
# overlapped the training set.
EMBARGO_S = 8 * 3600.0
CLUSTER_S = 7200.0


def _lgb():
    import lightgbm as lgb
    return lgb.LGBMRegressor(
        n_estimators=200, learning_rate=0.03, num_leaves=7,
        min_child_samples=100, subsample=0.8, subsample_freq=1,
        colsample_bytree=0.8, random_state=7, verbose=-1)


@dataclass(frozen=True)
class EdgeStatus:
    fitted: bool = False
    proven: bool = False
    n_rows: int = 0
    n_accepted: int = 0
    clusters: int = 0
    mean_r: Optional[float] = None
    t: Optional[float] = None
    r_h1: Optional[float] = None
    r_h2: Optional[float] = None
    auc_oos: Optional[float] = None
    fitted_at: Optional[float] = None
    refusal: str = "never fitted"


# ── The proof ────────────────────────────────────────────────────────────────

def prove(r: Sequence[float], ts: Sequence[float]) -> dict:
    """Is the mean of `r` positive beyond reasonable doubt?

    `r` are the out-of-sample results of the trades the model WOULD have
    taken, `ts` their trigger times. The t is cluster-robust over 2-hour
    blocks: trades riding the same move are one piece of evidence, not many.
    Proven means n >= MIN_ACCEPTED, clusters >= MIN_CLUSTERS, t >= MIN_T,
    and a positive mean in both halves of time taken separately.
    """
    n = len(r)
    out = {"n": n, "clusters": 0, "mean_r": None, "t": None,
           "r_h1": None, "r_h2": None, "proven": False, "refusal": ""}
    if n == 0:
        out["refusal"] = "it would have taken no trades"
        return out
    mean = sum(r) / n
    resid: dict = {}
    for x, t in zip(r, ts):
        k = int(t // CLUSTER_S)
        resid[k] = resid.get(k, 0.0) + (x - mean)
    var = sum(v * v for v in resid.values())
    tstat = (mean / (math.sqrt(var) / n)) if var > 0 else None
    order = sorted(range(n), key=lambda i: ts[i])
    half = n // 2
    h1 = [r[i] for i in order[:half]]
    h2 = [r[i] for i in order[half:]]
    out.update(clusters=len(resid), mean_r=round(mean, 4),
               t=round(tstat, 2) if tstat is not None else None,
               r_h1=round(sum(h1) / len(h1), 4) if h1 else None,
               r_h2=round(sum(h2) / len(h2), 4) if h2 else None)
    if n < MIN_ACCEPTED:
        out["refusal"] = f"only {n} out-of-sample trades taken; {MIN_ACCEPTED} needed"
    elif len(resid) < MIN_CLUSTERS:
        out["refusal"] = (f"its trades fall in {len(resid)} two-hour blocks; "
                          f"{MIN_CLUSTERS} needed")
    elif tstat is None or tstat < MIN_T:
        out["refusal"] = (f"mean {mean:+.3f}R at t={tstat if tstat is not None else 0:.2f}; "
                          f"t >= {MIN_T} needed")
    elif not (out["r_h1"] > 0 and out["r_h2"] > 0):
        out["refusal"] = (f"a losing half: {out['r_h1']:+.3f}R then "
                          f"{out['r_h2']:+.3f}R")
    else:
        out["proven"] = True
    return out


def walk_forward(ts_sorted: Sequence[float]) -> list[tuple[list[int], list[int]]]:
    """Expanding-window folds over time-sorted rows. Each test block is
    predicted by a model trained only on rows that ended EMBARGO_S before
    the block began."""
    n = len(ts_sorted)
    start = int(n * FIRST_TEST_FRAC)
    step = max(1, (n - start) // FOLDS)
    folds = []
    for k in range(FOLDS):
        a = start + k * step
        b = n if k == FOLDS - 1 else min(n, a + step)
        if a >= b:
            continue
        cut = ts_sorted[a] - EMBARGO_S
        train = [i for i in range(a) if ts_sorted[i] < cut]
        if len(train) < MIN_ACCEPTED:
            continue
        folds.append((train, list(range(a, b))))
    return folds


# ── The model ────────────────────────────────────────────────────────────────

def _kept(features: Sequence[float]) -> list[float]:
    return [float(features[i]) for i in KEPT]


class EdgeModel:
    def __init__(self, model_factory: Callable[[], Any] = _lgb):
        self._factory = model_factory
        self._model = None
        self.status = EdgeStatus()

    def fit(self, rows: Sequence[dict]) -> EdgeStatus:
        """Fit on `{features, tpl_r, trigger_time}` rows and judge the fit."""
        rows = sorted((r for r in rows or ()
                       if r.get("features") and r.get("tpl_r") is not None
                       and r.get("trigger_time")),
                      key=lambda r: float(r["trigger_time"]))
        n = len(rows)
        if n < MIN_ROWS:
            self._model = None
            self.status = EdgeStatus(fitted=False, n_rows=n, fitted_at=time.time(),
                                     refusal=f"{n} labelled trades; {MIN_ROWS} needed to judge a model")
            return self.status

        X = [_kept(r["features"]) for r in rows]
        y = [float(r["tpl_r"]) for r in rows]
        ts = [float(r["trigger_time"]) for r in rows]

        oos_pred, oos_idx = [], []
        for train, test in walk_forward(ts):
            m = self._factory().fit([X[i] for i in train], [y[i] for i in train])
            oos_pred.extend(float(p) for p in m.predict([X[i] for i in test]))
            oos_idx.extend(test)

        taken = [i for p, i in zip(oos_pred, oos_idx) if p >= 0]
        proof = prove([y[i] for i in taken], [ts[i] for i in taken])

        from backend.src.services.reversal_engine.meta_label import auc
        a = auc(oos_pred, [1 if y[i] > 0 else 0 for i in oos_idx]) if oos_idx else None

        self._model = self._factory().fit(X, y)
        self.status = EdgeStatus(
            fitted=True, proven=bool(proof["proven"]), n_rows=n,
            n_accepted=proof["n"], clusters=proof["clusters"],
            mean_r=proof["mean_r"], t=proof["t"], r_h1=proof["r_h1"],
            r_h2=proof["r_h2"], auc_oos=round(a, 4) if a is not None else None,
            fitted_at=time.time(), refusal=proof["refusal"])
        return self.status

    def decide(self, features: Optional[Sequence[float]]) -> tuple[bool, str, Optional[float]]:
        """(take it?, why not, predicted R). Refuses unless proven."""
        if self._model is None or not self.status.proven:
            return False, f"edge model not proven: {self.status.refusal}", None
        if not features or len(features) != len(FEATURE_NAMES):
            return False, "no feature vector to score", None
        try:
            pred = float(self._model.predict([_kept(features)])[0])
        except Exception as e:                    # noqa: BLE001
            return False, f"could not score: {e}", None
        if pred < 0:
            return False, f"predicted R {pred:+.3f} < 0", pred
        return True, "", pred


# ── Training rows ────────────────────────────────────────────────────────────

def rows_from_signals(signals: Sequence[dict]) -> list[dict]:
    """`re_signals` rows -> `{features, tpl_r, trigger_time}`. A row with no
    template label, no trigger or no stored features is left out."""
    out = []
    for s in signals or ():
        if s.get("tpl_r") is None or not s.get("trigger_time"):
            continue
        try:
            feats = pad_to_schema(json.loads(s.get("ml_features_json") or ""))
        except (TypeError, ValueError):
            continue
        if not feats:
            continue
        out.append({"features": feats, "tpl_r": float(s["tpl_r"]),
                    "trigger_time": float(s["trigger_time"])})
    return out


# ── The one live instance, and its daily refit ──────────────────────────────

_instance: Optional[EdgeModel] = None
_last_fit_date: Optional[str] = None
# Labelled rows at the last fit. The label sweep backfills history a day a
# minute, so a fit taken at the start of that would otherwise judge a
# handful of trades and keep that verdict until tomorrow.
_last_fit_n: int = 0
GROWTH_FACTOR = 1.25
GROWTH_MIN = 200


def get_instance() -> EdgeModel:
    global _instance
    if _instance is None:
        _instance = EdgeModel()
    return _instance


def decide(features: Optional[Sequence[float]]) -> tuple[bool, str, Optional[float]]:
    return get_instance().decide(features)


def _load_rows() -> list[dict]:
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    return rows_from_signals(re_db.get_ml_training_data())


def _count_labelled() -> int:
    """Rows with a template label, or 0 when the count cannot be read (the
    daily rule still refits then)."""
    try:
        from backend.src.services.reversal_engine import tpl_label_repo
        return tpl_label_repo.count_labelled()
    except Exception:                             # noqa: BLE001
        return 0


def _record(st: EdgeStatus) -> None:
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    re_db.set_config("edge_model_status", json.dumps(asdict(st)))
    log.info("[RE-Edge] fitted n=%d taken=%d clusters=%d mean=%s t=%s halves=%s/%s "
             "AUC(oos)=%s -> %s", st.n_rows, st.n_accepted, st.clusters,
             st.mean_r, st.t, st.r_h1, st.r_h2, st.auc_oos,
             "PROVEN" if st.proven else f"NOT PROVEN ({st.refusal})")


async def edge_model_refit_sweep(engine: Any, today: Optional[str] = None) -> None:
    """Refit once per London day, on the first pass after a restart (the
    model lives in memory, so the day it last fitted is process state), and
    whenever the labelled rows have grown by a quarter and at least
    GROWTH_MIN since the last fit."""
    global _last_fit_date, _last_fit_n
    today = today or datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")
    n_now = await asyncio.to_thread(_count_labelled)
    grown = n_now >= max(_last_fit_n * GROWTH_FACTOR, _last_fit_n + GROWTH_MIN)
    if _last_fit_date == today and not grown:
        return
    rows = await asyncio.to_thread(_load_rows)
    fresh = EdgeModel()
    st = await asyncio.to_thread(fresh.fit, rows)
    global _instance
    _instance = fresh
    # After the fit, not before: a read that raised retries next minute
    # rather than leaving the day unfitted.
    _last_fit_date = today
    _last_fit_n = n_now
    _record(st)
