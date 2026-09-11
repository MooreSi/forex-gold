"""What the model learns from: the label, and the training set.

Split out of `ml_engine/__init__.py` on 2026-09-11 (785 lines against a hard
800). Verbatim -- the label arithmetic, the clamp and the right-pad are
unchanged.

A safe seam because nothing here touches the module's mutable state: no model,
no counter, no `global`. It reads rows through `reversal_engine_repo` and the
vector's shape through `_feature_schema`, and returns plain lists. Everything
in `__init__` that rebinds `_model_batch`, `_model_online`, `_labeled_count`,
`_ref_level_stats`, `_train_history` or `_data_dir` stays there, because
splitting a module that rebinds a global forks that state.

`_DOLLARS_PER_POINT` came with the label it belongs to. `ml_engine` re-exports
all three constants and all three functions:
`tests/reversal_engine/test_ml_realised_r_label.py` reads `m._realised_r` and
must pass unmodified.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ._feature_schema import FEATURE_NAMES, _FEATURE_NEUTRAL

_log = logging.getLogger(__name__)

# Dollars per point for a virtual signal. Mirrors reversal_engine_manage.py's
# `gross = pnl_pts * _VIRTUAL_LOT * 100` -- duplicated as a constant rather
# than imported because that module imports this one (circular otherwise).
_VIRTUAL_LOT = 0.1
_DOLLARS_PER_POINT = _VIRTUAL_LOT * 100

# Guard against a corrupt sl_dist/net_pnl_dollars producing an absurd label.
# Deliberately wider than any real observation to date (worst -5.75R, best
# +5.01R) so it never silently truncates a genuine tail -- the whole point of
# this label is that real tails are bigger than the old one could express.
_R_LABEL_CLAMP = 12.0


def _realised_r(row: dict) -> Optional[float]:
    """Realised R for a closed signal: actual net dollars banked (partials
    included) divided by the dollars that signal's own initial stop put at
    risk. Returns None when the row can't express it.

    This is what the model is trained on as of v5. It differs from the
    planned rr_tp1 in three ways that all matter: it counts scale-outs at the
    fraction actually closed rather than the full planned target, it charges
    spread/commission/slippage, and it lets a loss exceed -1.0R when the stop
    fills past sl_dist (which on real rows it routinely does)."""
    try:
        risk = float(row.get("sl_dist") or 0.0) * _DOLLARS_PER_POINT
        if risk <= 0:
            return None
        net = row.get("net_pnl_dollars")
        if net is None:
            return None
        return max(-_R_LABEL_CLAMP, min(_R_LABEL_CLAMP, float(net) / risk))
    except (TypeError, ValueError):
        return None


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
            # Older rows were labeled under a previous _version with fewer
            # features. Discarding them (the pre-v6 behaviour) meant every
            # feature addition silently threw the entire training history
            # away -- at v6 that would have been all 752 labeled signals,
            # leaving the model with nothing until months of new ones
            # accumulated. Pad them with the documented neutral for each
            # missing feature instead, which is truthful: those rows really
            # do have no FVG context recorded. Only right-padding is valid,
            # and only because features are append-only (see
            # _FEATURE_NEUTRAL). A vector LONGER than the current schema is
            # from a newer build and still can't be interpreted, so it is
            # still skipped.
            if len(f) > len(FEATURE_NAMES):
                continue
            if len(f) < len(FEATURE_NAMES):
                f = f + [_FEATURE_NEUTRAL.get(n, 0.0)
                         for n in FEATURE_NAMES[len(f):]]
            outcome = r.get("outcome", "")
            if outcome not in ("win", "loss", "be"):
                continue
            label = _realised_r(r)
            if label is None:
                continue
            X.append(f)
            y.append(label)
        return X, y
    except Exception as exc:
        _log.debug("[RE-ML] training data error: %s", exc)
        return [], []


def _labeled_count_from_db() -> int:
    """How many closed signals are actually trainable right now.

    _labeled_count is an in-memory counter that _save_all() persists, and
    record_ref_signal() also triggers a save -- so any process that saves
    before it has a real count (a fresh module whose meta load found nothing,
    or the first run after a _version bump discards the old meta) writes a 0
    over a perfectly good number. That then suppresses batch retraining until
    _RETRAIN_EVERY fresh outcomes accumulate, even though hundreds of
    trainable rows are sitting in the DB. Observed live 2026-07-31: a
    retrained model with n=576 came back as labeled=0 with the ML panel
    claiming it needed 15 more signals before its first training.

    The DB is the real source of truth, so use it to repair the counter
    rather than trusting whatever was last written to the pickle."""
    try:
        X, _ = _get_training_data()
        return len(X)
    except Exception:
        return 0
