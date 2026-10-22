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
import math
from typing import Optional

from ._feature_schema import FEATURE_NAMES, _FEATURE_NEUTRAL, pad_to_schema

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
    # An executed close stores the BROKER's profit, at the real lot and under
    # the template's stop, not sl_dist at the 0.1 virtual lot this divides by.
    # The row carries neither the lot nor the initial stop, so it is left out
    # rather than read on the wrong scale (reversal-engine/250, 2026-09-30).
    if row.get("live_exec_status") == "executed":
        return None
    try:
        risk = float(row.get("sl_dist") or 0.0) * _DOLLARS_PER_POINT
        if risk <= 0 or not math.isfinite(risk):
            return None
        net = row.get("net_pnl_dollars")
        if net is None:
            return None
        value = float(net) / risk
        return max(-_R_LABEL_CLAMP, min(_R_LABEL_CLAMP, value)) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


# docs/todo/017 item 1. Provisional: a two-week half-life and a floor of a
# quarter. The offline replay (tools/ml_weight_replay.py) is what says whether
# any setting beats today's positional weights; until it does, the switch
# stays off and these are only what "on" would mean.
DECAY_HALF_LIFE_DAYS = 14.0
DECAY_FLOOR = 0.25


def _learning_switches() -> tuple[bool, bool]:
    """(re_ml_time_decay_enabled, re_ml_learn_from_executed). Off on any failure."""
    try:
        from backend.src.db import database as _db
        from backend.src.services.risk import capability_gates as _caps
        rs = _db.get_risk_settings() or {}
        return bool(_caps.re_ml_time_decay_enabled(rs)), bool(_caps.re_ml_learn_from_executed(rs))
    except Exception:
        return False, False


def effective_n(n: int, weights: Optional[list] = None) -> float:
    """How many rows a fit's weights are worth (Kish). With no weights, the
    positional ones `_fit_batch` applies by default."""
    from backend.src.services.market import sample_weights as _sw
    w = weights if weights is not None else [
        math.exp(-0.017 * (n - i)) for i in range(n)]
    return round(_sw.effective_size(w), 1)


def learning_description() -> dict:
    """What the next fit learns from, in words, for the "Is it learning?" panel."""
    decay_on, executed_on = _learning_switches()
    return {
        "label": "realised R (net over the signal's own risk)",
        "learns_from_executed": executed_on,
        "weights": (f"by age: half-life {DECAY_HALF_LIFE_DAYS:g} days, floor "
                    f"{DECAY_FLOOR:g}, overlap discounted") if decay_on
        else "positional (newest rows weighted most)",
    }


def _span(row: dict) -> tuple[float, float]:
    """When the label was live: trigger (or creation) to close."""
    start = float(row.get("trigger_time") or row.get("created_at") or 0.0)
    end = float(row.get("close_time") or start)
    return start, max(start, end)


def training_label(row: dict, executed_risk: Optional[dict] = None) -> Optional[float]:
    """The label the model learns for `row`: `_realised_r` for a virtual row;
    for an executed one, its core trade's net over its recorded initial risk
    when `executed_risk` holds it (docs/todo/017 item 2), else None."""
    if row.get("live_exec_status") != "executed":
        return _realised_r(row)
    if not executed_risk:
        return None
    pair = executed_risk.get(str(row.get("vantage_signal_id") or ""))
    if pair is None:
        return None
    from backend.src.services.reversal_engine import executed_label as _xl
    return _xl.label(*pair)


def executed_risk_for(rows: list) -> dict:
    """The core trades' (net, initial_risk) for every executed row, or {}."""
    ids = [r.get("vantage_signal_id") for r in rows
           if r.get("live_exec_status") == "executed" and r.get("vantage_signal_id")]
    if not ids:
        return {}
    from backend.src.services.reversal_engine import executed_label as _xl
    try:
        return _xl.fetch_closed_risk(ids)
    except Exception as exc:
        _log.debug("[RE-ML] executed risk lookup error: %s", exc)
        return {}


def _get_training_data():
    """Pull closed signals with features from DB. Returns (X, y) where y is R-multiple."""
    X, y, _ = _get_training_set()
    return X, y


def _get_training_set():
    """(X, y, weights). `weights` is None unless `re_ml_time_decay_enabled`
    is on, which leaves the fit on its positional weights exactly as before.
    Executed rows join only with `re_ml_learn_from_executed` on."""
    try:
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        from backend.src.services.reversal_engine import vol_regime as _vol
        # Read once, so every row in this pass has the same width. On, any
        # row the minute sweep has not reached is measured first: the stored
        # history is reused, not read as neutral (docs/todo/016).
        vol_on = _vol.feature_enabled()
        if vol_on:
            try:
                while _vol.backfill():
                    pass
            except Exception as exc:
                _log.debug("[RE-ML] vol_regime backfill error: %s", exc)
        decay_on, executed_on = _learning_switches()
        rows = re_db.get_ml_training_data()
        # Oldest first: _fit_batch weights row i by its position, and the
        # query has no ORDER BY to promise that.
        rows = sorted(rows, key=lambda r: r.get("id") or 0)
        # The other node's rows, when a merge's benefit check kept them
        # (docs/todo/026). Off, this is `rows` itself.
        from backend.src.services.ml_merge import union as _merge
        rows = _merge.with_imported("re", rows)
        return training_set_from_rows(rows, vol_on, decay_on, executed_on)
    except Exception as exc:
        _log.debug("[RE-ML] training data error: %s", exc)
        return [], [], None


def training_set_from_rows(rows: list, vol_on: bool, decay_on: bool,
                           executed_on: bool):
    """(X, y, weights) for `rows`, already oldest first. Shared with the merge's
    benefit check, so it scores exactly what training would fit."""
    from backend.src.services.reversal_engine import vol_regime as _vol
    risk = executed_risk_for(rows) if executed_on else None
    X, y, spans = [], [], []
    for r in rows:
        f = stored_vector(r)
        if f is None:
            continue
        outcome = r.get("outcome", "")
        if outcome not in ("win", "loss", "be"):
            continue
        label = training_label(r, risk)
        if label is None:
            continue
        X.append(f + _vol.row_tail(r, vol_on))
        y.append(label)
        spans.append(_span(r))
    weights = None
    if decay_on and X:
        from backend.src.services.market import sample_weights as _sw
        weights = _sw.training_weights(spans, DECAY_HALF_LIFE_DAYS, DECAY_FLOOR)
    return X, y, weights


def stored_vector(row: dict) -> Optional[list]:
    """A closed signal's stored feature vector, as the model should read it:
    right-padded to the current schema, and with the zero-substitution
    repaired where the row's own columns can say what the value really was.
    None when there is no vector or it cannot be interpreted.

    Shared by the batch training set and the online learner, so both read an
    old row the same way.
    """
    feats = row.get("ml_features_json")
    if not feats:
        return None
    try:
        f = json.loads(feats)
    except Exception:
        return None
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
    if not isinstance(f, list):
        return None
    try:
        if not all(math.isfinite(float(v)) for v in f):
            return None
    except (TypeError, ValueError):
        return None
    f = pad_to_schema(f)
    if f is None:
        return None
    return _repair_zero_substitution(f, row)


_I_REGIME = FEATURE_NAMES.index("regime_score")
_I_DIST = FEATURE_NAMES.index("distance_norm")


def _repair_zero_substitution(f: list, row: dict) -> list:
    """Undo `x or default` where the row can prove the real value was zero.

    Until 2026-10-01 extract_features read regime_score as `x or 0.5` and the
    level distance as `x or 5`, so a ranging market (ADX < 14, regime 0.0) was
    stored as 0.5 and a signal sitting on its level as 5 points away. Both are
    recomputable from columns the same row holds, so they are corrected here
    rather than left as 7,000 rows that contradict every new one. Only an
    exact match on the substituted value is touched; anything else is left.
    The other substitutions (news 0 -> 1.0, FVG distance 0 -> 5.0, minutes
    since REF 0 -> 240) cannot be recovered from the row and stay as stored.
    """
    adx, atr = row.get("adx"), row.get("atr")
    if f[_I_REGIME] == 0.5 and adx is not None:
        from backend.src.db import database as _cdb
        if _cdb.get_regime_score(float(adx), float(atr or 5)) == 0.0:
            f[_I_REGIME] = 0.0
    price, level = row.get("price_at_signal"), row.get("level_price")
    if price and level and float(price) == float(level):
        atr_used = float(atr or 8)
        if f[_I_DIST] == min(5.0 / max(atr_used, 1.0), 5.0):
            f[_I_DIST] = 0.0
    return f


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
