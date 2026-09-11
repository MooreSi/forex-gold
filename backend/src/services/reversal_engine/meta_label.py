"""The meta-labeller: whether to act, as a question of its own.

Sections 1.3 and 5.2 of `docs/todo/reversal-engine/200`.

`re_ml` regresses R-multiple over the whole signal population and blocks
below zero. `re_ml_meta.pkl` records `mean_r = -0.083` at every retrain, so as
the model improves it blocks more -- 31% of signals executed on 2026-09-02,
11% on 2026-09-07. A gate converging on "trade nothing" is not a filter, it
is a verdict on the generator.

Splitting the question is the standard answer. The level detector keeps
deciding DIRECTION. This decides only whether to act, and its label is binary
-- did the trade clear its own execution cost -- which is learnable on the
rows that exist in a way a continuous R regression is not.

Three disciplines, each pinned by a test:

  * **Purged, embargoed folds.** Labels here overlap (a two-hour pending
    window, up to six concurrent signals), so plain k-fold leaks and reports
    a model that does not exist.
  * **Uniqueness weighting.** Overlapping samples are not independent
    observations and are not counted as such.
  * **It refuses to arm** when it cannot beat a coin out of sample, and an
    unarmed model has NO OPINION rather than a neutral one. `pro_model`
    already holds this line for the same reason.

This module decides nothing on its own. `should_take` returns True for an
unarmed model, and the caller is expected to check `status.ready` and its own
default-off toggle before consulting it at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from backend.src.services.market import validation as val

log = logging.getLogger("reversal_engine")

DEFAULT_MIN_SAMPLES = 200
DEFAULT_MIN_AUC = 0.55
DEFAULT_FOLDS = 4
# In seconds, and deliberately longer than the 1800s level cooldown: a
# signal on the same level inside that window is close to the same trade.
DEFAULT_EMBARGO_S = 3600.0


@dataclass(frozen=True)
class MetaLabelStatus:
    ready: bool
    n_samples: int
    n_folds: int
    auc_oos: Optional[float]
    auc_in_sample: Optional[float]
    refusal: str = ""


def label_for(realised_r: Optional[float],
              cost_r: Optional[float]) -> Optional[int]:
    """1 when the trade cleared its own execution cost, else 0.

    Not "did it win". A +0.05R win that cost 0.18R to execute is a trade the
    system should not have taken, and labelling it a success teaches the gate
    to keep taking it. None when the cost was never measured -- an unmeasured
    cost is not a zero cost, and guessing here would quietly relabel the very
    population this is meant to learn from. See `broker/tca.py`.
    """
    if realised_r is None or cost_r is None:
        return None
    return 1 if float(realised_r) > float(cost_r) else 0


def auc(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Rank-based area under the ROC curve (Mann-Whitney U).

    Hand-rolled rather than imported: it is eight lines, it must work when
    one class is absent (returning None rather than 0.5, because "no
    positives to separate" is not "no separation"), and this module must be
    able to report honestly on a fold that happens to be one-sided.
    """
    pairs = [(s, l) for s, l in zip(scores, labels) if s is not None]
    pos = [s for s, l in pairs if l == 1]
    neg = [s for s, l in pairs if l == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for q in neg:
            wins += 1.0 if p > q else (0.5 if p == q else 0.0)
    return wins / (len(pos) * len(neg))


class MetaLabeller:
    def __init__(self, min_samples: int = DEFAULT_MIN_SAMPLES,
                 min_auc: float = DEFAULT_MIN_AUC,
                 folds: int = DEFAULT_FOLDS,
                 embargo_s: float = DEFAULT_EMBARGO_S):
        self.min_samples = min_samples
        self.min_auc = min_auc
        self.folds = folds
        self.embargo_s = embargo_s
        self._model = None
        self._scaler = None
        self.status = MetaLabelStatus(False, 0, 0, None, None,
                                      "never fitted")

    # ── fitting ───────────────────────────────────────────────────────────

    def _new_model(self):
        from sklearn.linear_model import LogisticRegression
        # Regularised hard and deliberately linear. The population is a few
        # thousand overlapping rows; a model with the capacity to memorise
        # them would, and the purged folds would then be the only thing
        # standing between that and a live gate.
        return LogisticRegression(C=0.5, max_iter=1000, solver="lbfgs")

    def fit(self, rows: Sequence[dict]) -> MetaLabelStatus:
        """Fit on `{features, realised_r, cost_r, open_time, close_time}`."""
        usable = []
        for r in rows or ():
            y = label_for(r.get("realised_r"), r.get("cost_r"))
            feats = r.get("features")
            if y is None or not feats:
                continue
            usable.append((list(feats), y,
                           float(r.get("open_time") or 0.0),
                           float(r.get("close_time") or 0.0)))

        if len(usable) < self.min_samples:
            self.status = MetaLabelStatus(
                False, len(usable), 0, None, None,
                f"sample of {len(usable)} labelled trades is below the "
                f"{self.min_samples} this will not arm without")
            self._model = None
            return self.status

        X = [u[0] for u in usable]
        y = [u[1] for u in usable]
        spans = [(u[2], u[3]) for u in usable]
        weights = val.uniqueness_weights(spans)

        folds = val.purged_kfold(len(usable), self.folds, spans,
                                 embargo=self.embargo_s)
        oos_scores: list[float] = []
        oos_labels: list[int] = []
        used_folds = 0
        for train_idx, test_idx in folds:
            if len(train_idx) < 20 or not test_idx:
                continue
            if len({y[i] for i in train_idx}) < 2:
                continue
            model = self._new_model()
            model.fit([X[i] for i in train_idx], [y[i] for i in train_idx],
                      sample_weight=[weights[i] for i in train_idx])
            probs = model.predict_proba([X[i] for i in test_idx])[:, 1]
            oos_scores.extend(float(p) for p in probs)
            oos_labels.extend(y[i] for i in test_idx)
            used_folds += 1

        auc_oos = auc(oos_scores, oos_labels)

        final = self._new_model()
        final.fit(X, y, sample_weight=weights)
        auc_is = auc([float(p) for p in final.predict_proba(X)[:, 1]], y)

        if used_folds < 2 or auc_oos is None:
            self._model = None
            self.status = MetaLabelStatus(
                False, len(usable), used_folds, auc_oos, auc_is,
                "not enough clean out-of-sample folds survived purging to "
                "judge this model")
            return self.status

        if auc_oos < self.min_auc:
            self._model = None
            self.status = MetaLabelStatus(
                False, len(usable), used_folds, auc_oos, auc_is,
                f"out-of-sample AUC {auc_oos:.3f} does not clear "
                f"{self.min_auc:.2f}: this model cannot beat a coin, and an "
                f"uninformative gate must read the same as no gate")
            return self.status

        self._model = final
        self.status = MetaLabelStatus(True, len(usable), used_folds,
                                      auc_oos, auc_is)
        log.info("[RE-Engine] meta-labeller armed: n=%d folds=%d AUC(oos)=%.3f",
                 len(usable), used_folds, auc_oos)
        return self.status

    # ── using it ──────────────────────────────────────────────────────────

    def probability(self, features: Sequence[float]) -> Optional[float]:
        """P(this trade clears its cost), or None when not armed."""
        if self._model is None:
            return None
        try:
            return float(self._model.predict_proba([list(features)])[0][1])
        except Exception as e:                    # noqa: BLE001
            log.debug("meta-labeller scoring failed: %s", e)
            return None

    def should_take(self, features: Sequence[float], threshold: float) -> bool:
        """True unless an ARMED model scores this below `threshold`.

        An unarmed model blocks nothing. That is not failing open: the caller
        consults this only when its own default-off toggle is on and
        `status.ready` is True, and this return value exists so that a model
        going unready at runtime cannot silently halt trading.
        """
        p = self.probability(features)
        return True if p is None else p >= threshold


# ── The one live instance ─────────────────────────────────────────────────
#
# A module singleton rather than state on the engine: the engine is recreated
# by the watchdog's `start()` re-entry, and refitting a model on every restart
# would mean the gate's behaviour depended on uptime.
_instance: Optional[MetaLabeller] = None


def get_instance() -> MetaLabeller:
    global _instance
    if _instance is None:
        _instance = MetaLabeller()
    return _instance


def score_signal(sig: dict) -> Optional[float]:
    """P(this signal clears its cost), or None when the model has no opinion.

    None covers every case where the answer is unknown: never fitted,
    refused to arm, features unavailable, scoring raised. The caller must
    treat all of them the same way -- as no information, not as a negative
    verdict. A model that has not learned anything must not be able to stop
    the app trading.

    Until `broker/tca.py` has costed a few hundred closed trades there is
    nothing to label, so this returns None however the switch is set. That
    is the intended order: measure the cost, then gate on it.
    """
    model = get_instance()
    if not model.status.ready:
        return None
    try:
        from backend.src.services.reversal_engine import ml_engine as re_ml
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        feats = re_ml.extract_features(sig, re_db.get_recent_win_rate(20))
    except Exception as e:                       # noqa: BLE001
        log.debug("meta-labeller could not build features: %s", e)
        return None
    if not feats:
        return None
    return model.probability(feats)
