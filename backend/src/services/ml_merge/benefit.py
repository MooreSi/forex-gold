"""Is the other node's history worth learning from? (docs/todo/026)

Asked per dataset, on this node, with each model's own fitting code:

  1. Own rows and the imported rows that are not an event this node already
     holds, oldest first. The newest 20% are the held-out set.
  2. Model A trains on this node's own rows before the cut, model B on every
     row before the cut, with the weights training would use.
  3. Both are scored on the held-out rows:
       re   the gate's effect: realised R per signal with the gate (predicted
            R < 0 blocked, counted as 0) minus R per signal taking them all
       bo   AUC of the prediction against win/loss
       pro  AUC of pro-likeness against pro/background
  4. The switch goes on when B scores at least as well as A. With no A to
     compare (too few own rows before the cut), B must beat doing nothing.

A held-out set of a few hundred rows is noisy. This guards against a merge
that makes the model measurably worse on the newest data; it does not prove a
merge helps. The held-out set is the newest data whoever generated it: after
a two-way merge both nodes hold the same union and reach the same verdict.

Runs model fits: call it off the event loop.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.src.services.ml_merge import kinds, repo, union

log = logging.getLogger(__name__)

HOLDOUT = 0.2
MIN_HOLDOUT = 30
NEUTRAL = {"re": 0.0, "bo": 0.5, "pro": 0.5}
METRIC = {"re": "gate uplift (R per signal)", "bo": "AUC", "pro": "AUC"}
_MIN_CLASS = 10


def decide(a: Optional[float], b: Optional[float], neutral: float) -> bool:
    if b is None:
        return False
    if a is None:
        return b > neutral
    return b >= a


def _cut(rows: list) -> tuple[list, list]:
    cut = int(len(rows) * (1.0 - HOLDOUT))
    return rows[:cut], rows[cut:]


# ── Reversal ─────────────────────────────────────────────────────────────────

def _uplift(preds, y: list) -> float:
    n = len(y)
    gated = sum(v for p, v in zip(preds, y) if float(p) >= 0.0) / n
    return gated - sum(y) / n


def _own_reversal() -> list[dict]:
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    return sorted(re_db.get_ml_training_data(), key=lambda r: r.get("id") or 0)


def _reversal(own: list[dict], fresh: list[dict]) -> tuple:
    from backend.src.services.reversal_engine import ml_engine as ml
    from backend.src.services.reversal_engine import vol_regime as _vol
    from backend.src.services.reversal_engine.ml_engine import _training_data as td
    decay_on, executed_on = td._learning_switches()
    vol_on = _vol.feature_enabled()
    own_ids = {id(r) for r in own}
    before, after = _cut(union.merge("re", own, fresh))
    Xh, yh, _ = td.training_set_from_rows(after, vol_on, False, executed_on)
    if len(yh) < MIN_HOLDOUT:
        return None, None, len(yh)

    def score(rows):
        X, y, w = td.training_set_from_rows(rows, vol_on, decay_on, executed_on)
        if len(X) < ml._MIN_TRAIN:
            return None
        model = ml._fit_batch(X, y, *([] if w is None else [w]))[0]
        return _uplift(model.predict(Xh), yh)

    return (score([r for r in before if id(r) in own_ids]), score(before), len(yh))


# ── Breakout ─────────────────────────────────────────────────────────────────

def _bo_fit(X, y, w):
    from backend.src.services.breakout_signal import ml_engine as bo_ml
    if bo_ml._LGB:
        return bo_ml._fit_lgb(X, y, w)
    from sklearn.ensemble import RandomForestRegressor
    return RandomForestRegressor(n_estimators=80, max_depth=5,
                                 random_state=42).fit(X, y, sample_weight=w)


def _breakout(own: list[dict], fresh: list[dict]) -> tuple:
    import numpy as np
    from backend.src.services.breakout_signal import ml_engine as bo_ml
    from backend.src.services.breakout_signal import ml_labels as _lb
    decay_on, _ = _lb.switches()
    mode = _lb.mode()
    own_ids = {id(r) for r in own}
    before, after = _cut(union.merge("bo", own, fresh))
    Xh, yh, _ = bo_ml.training_arrays(after, False, mode)
    truth = [1 if v > 0 else 0 for v in yh]
    if len(yh) < MIN_HOLDOUT or len(set(truth)) < 2:
        return None, None, len(yh)

    def score(rows):
        X, y, w = bo_ml.training_arrays(rows, decay_on, mode)
        if len(X) < bo_ml.MIN_TRAIN_SAMPLES:
            return None
        model = _bo_fit(X, y, w)
        return _lb.auc(truth, [float(p) for p in model.predict(np.array(Xh, dtype=float))])

    return (score([r for r in before if id(r) in own_ids]), score(before), len(yh))


# ── Pro corpus ───────────────────────────────────────────────────────────────

def _own_pro() -> list[dict]:
    from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
    return pro_corpus.rows(background=False) + pro_corpus.rows(background=True)


def _pro(own: list[dict], fresh: list[dict]) -> tuple:
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from backend.src.services.reversal_engine import pro_model
    own_ids = {id(r) for r in own}
    rows = own + fresh
    is_bg = [str(r.get("tg_message_id") or "").startswith("bg-")
             or r.get("stage") == "background" for r in rows]
    X, y, w, _spread, times, used = pro_model.dataset_from_rows(
        [r for r, bg in zip(rows, is_bg) if not bg], [r for r, bg in zip(rows, is_bg) if bg])
    order = sorted(range(len(y)), key=lambda i: (times[i], i))
    before, after = _cut(order)
    if len(after) < MIN_HOLDOUT or len({y[i] for i in after}) < 2:
        return None, None, len(after)
    Xa, ya, wa = np.array(X, dtype=float), np.array(y), np.array(w, dtype=float)

    def score(idx):
        idx = np.array(idx, dtype=int)
        if len(idx) == 0 or min(int((ya[idx] == 1).sum()), int((ya[idx] == 0).sum())) < _MIN_CLASS:
            return None
        model = pro_model.make_model()
        model.fit(Xa[idx], ya[idx], sample_weight=wa[idx])
        te = np.array(after, dtype=int)
        return float(roc_auc_score(ya[te], model.predict_proba(Xa[te])[:, 1]))

    return (score([i for i in before if id(used[i]) in own_ids]), score(before), len(after))


_OWN = {"re": _own_reversal, "pro": _own_pro}
_SCORE = {"re": _reversal, "bo": _breakout, "pro": _pro}


def _own_rows(kind: str) -> list[dict]:
    if kind == "bo":
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        return bdb.get_ml_training_data()
    return _OWN[kind]()


def evaluate(kind: str) -> dict:
    """Run the check for `kind`, store the verdict and switch, return the report."""
    if kind not in repo.KINDS:
        raise ValueError(f"unknown training dataset {kind!r}")
    own = _own_rows(kind)
    peer = repo.imported(kind)
    fresh = kinds.drop_duplicates(kind, own, peer)
    report = {"kind": kind, "metric": METRIC[kind], "n_own": len(own),
              "n_imported": len(peer), "n_new": len(fresh),
              "a": None, "b": None, "n_holdout": 0}
    if not fresh:
        use = False
        report["reason"] = ("nothing new to add: no imported rows" if not peer else
                            "nothing new to add: every imported row is already here")
    else:
        try:
            a, b, n_hold = _SCORE[kind](own, fresh)
        except Exception as exc:
            log.warning("[ml_merge] %s benefit check failed: %s", kind, exc)
            a, b, n_hold = None, None, 0
        report.update(a=a, b=b, n_holdout=n_hold)
        use = decide(a, b, NEUTRAL[kind])
        if b is None:
            report["reason"] = "not enough rows to measure; imported rows kept but unused"
        elif use:
            report["reason"] = ("merged history scores at least as well on the newest "
                                "rows; training now includes it")
        else:
            report["reason"] = ("merged history scores worse on the newest rows; "
                                "imported rows kept but unused")
    report["use"] = use
    repo.set_decision(kind, use, report)
    log.info("[ml_merge] %s: use=%s a=%s b=%s holdout=%s", kind, use,
             report["a"], report["b"], report["n_holdout"])
    return report
