"""Trend PA's model: a logistic regression on the setup, and when to trust it.

Deliberately small. Fourteen numbers describe each setup (strategy.evaluate's
`features`); the label is whether the trade reached its target. The model is
fitted on the oldest 75% of closed trades and scored on the newest 25% -- a
TIME-ordered holdout, because a random split lets next month's trades teach
the model about last month's.

**It only gets an opinion once it has earned one**: at least MIN_SAMPLES
closed trades and a holdout AUC of at least MIN_AUC. Measured 2026-09-29 on
533 replayed trades, the same model scored 0.49-0.53 out of sample -- no
better than a coin -- so on today's evidence it stays unarmed, and the panel
says so. An unarmed model returns None and the gate lets the trade through:
the strategy's own rules are the filter, and the engine's live switch is off
by default.

Once armed, the gate asks for positive expected R at the model's win
probability: p * rr - (1 - p) - cost_r > 0.
"""
from __future__ import annotations

import logging
import pickle
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("trend_pa")

FEATURES = (
    "trend_step_atr", "ema_dist_atr", "body_frac", "wick_frac", "is_engulfing",
    "level_dist_atr", "level_role_reversal", "pullback_atr", "risk_atr",
    "atr_ratio", "hour_sin", "hour_cos", "overlap", "is_buy",
)
MIN_SAMPLES = 60
MIN_AUC = 0.55
HOLDOUT = 0.25

_UNARMED = {"armed": False, "n": 0, "auc": None, "why": "not fitted yet",
            "fitted_at": None, "base_rate": None, "weights": {}}


def _vector(features: dict) -> list:
    return [float(features.get(k, 0.0) or 0.0) for k in FEATURES]


def _auc(y: list, p: list) -> Optional[float]:
    """Rank AUC. None when the sample holds only one class."""
    pos = [s for s, t in zip(p, y) if t]
    neg = [s for s, t in zip(p, y) if not t]
    if not pos or not neg:
        return None
    ranked = sorted(zip(p, y))
    ranks, i = {}, 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2.0 + 1
        i = j + 1
    rank_pos = sum(ranks[k] for k, (_, t) in enumerate(ranked) if t)
    return (rank_pos - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))


def _pipeline():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=1000))


class Model:
    def __init__(self):
        self._pipe = None
        self.state: dict = dict(_UNARMED)

    def fit(self, rows: list) -> dict:
        """Fit on closed trades ({features, outcome, closed_at}); return state."""
        rows = sorted((r for r in rows if r.get("features")),
                      key=lambda r: float(r.get("closed_at") or 0))
        n = len(rows)
        base = {**_UNARMED, "n": n, "fitted_at": time.time()}
        if n < MIN_SAMPLES:
            self._pipe, self.state = None, {**base, "why": f"{n} of {MIN_SAMPLES} rows needed"}
            return self.state
        X = [_vector(r["features"]) for r in rows]
        y = [r["outcome"] == "win" for r in rows]
        k = int(n * (1 - HOLDOUT))
        if len(set(y[:k])) < 2:
            self._pipe, self.state = None, {**base, "why": "training rows hold one outcome only"}
            return self.state
        probe = _pipeline().fit(X[:k], y[:k])
        auc = _auc(y[k:], list(probe.predict_proba(X[k:])[:, 1]))
        full = _pipeline().fit(X, y)
        coefs = full[-1].coef_[0]
        weights = {f: round(float(c), 4) for f, c in zip(FEATURES, coefs)}
        armed = auc is not None and auc >= MIN_AUC
        why = ("armed" if armed else
               "holdout has one outcome only" if auc is None else
               f"holdout AUC {auc:.3f} below {MIN_AUC}")
        self._pipe = full if armed else None
        self.state = {**base, "armed": armed, "auc": auc, "why": why,
                      "base_rate": sum(y) / n, "weights": weights}
        log.info("[TPA-ML] fitted on %d: %s", n, why)
        return self.state

    def predict(self, features: dict) -> Optional[float]:
        """P(win), or None when unarmed."""
        if self._pipe is None:
            return None
        return float(self._pipe.predict_proba([_vector(features)])[0, 1])

    def save(self, path: Path) -> None:
        with open(path, "wb") as fh:
            pickle.dump({"pipe": self._pipe, "state": self.state}, fh)

    @classmethod
    def load(cls, path: Path) -> "Model":
        m = cls()
        try:
            with open(path, "rb") as fh:
                blob = pickle.load(fh)
            m._pipe, m.state = blob["pipe"], dict(blob["state"])
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("[TPA-ML] could not load %s (%s) -- starting unarmed", path, e)
        return m


def gate_passes(p: Optional[float], rr: float, cost_r: float) -> bool:
    """True when the model has no opinion or expects a positive R."""
    if p is None:
        return True
    return p * rr - (1.0 - p) - cost_r > 0
