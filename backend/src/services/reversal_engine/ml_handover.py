"""When the ML model version changes, hand the old model over — do not bin it.

A bump used to leave `_model_batch`/`_model_online` unset until the next
retrain, and `predict()` returns None with no model. The gate in
reversal_engine_live_execute reads
    if fresh_prob is not None and float(fresh_prob) < _ML_BLOCK_THRESHOLD
so a None prediction does not block: **the ML gate fails OPEN**, and every
signal executed unfiltered from the moment of the bump until training
finished. v9 shipped Saturday 2026-09-05; the first v9 retrain was Monday
09:27.

The training DATA was never the problem -- `_collect_training_data` re-reads
every closed signal from the database and right-pads older rows -- so all a
bump ever cost was the fitted model and that open window.

So the previous model keeps scoring until a retrain replaces it, seeing only
the leading features it was fitted on. Valid because features are
append-only (`_FEATURE_NEUTRAL`'s contract): the first 33 of a v9 vector ARE
a v8 vector.

Only from v5. v5 replaced the LABEL -- was `rr_tp1 if win else -1.0`, now
realised net R -- so a v4 model predicts a different quantity, and the gate
compares that number to zero. Handing over across a label change would feed
the gate a number that means something else.
_LABEL_EPOCH = 5
_legacy_width: Optional[int] = None
"""
from __future__ import annotations

from typing import Optional

# v5 replaced the label (was `rr_tp1 if win else -1.0`, now realised net R), so
# a model fitted before it predicts a different quantity entirely.
LABEL_EPOCH = 5


def version_num(tag) -> Optional[int]:
    """The trailing integer of a version tag, or None if it is not one.

    None means "refuse to hand over", which is the safe direction: it is
    simply the old behaviour of retraining from scratch.
    """
    try:
        head, _, num = str(tag).rpartition("_v")
        if not head or not num:
            return None
        return int(num)
    except (TypeError, ValueError):
        return None


def may_hand_over(old_version) -> bool:
    """May a model fitted under `old_version` keep serving under this one?"""
    n = version_num(old_version)
    return n is not None and n >= LABEL_EPOCH


# Set while a previous version's model is still scoring; None means the model
# matches the current schema. State lives here rather than in ml_engine, which
# sits on the 800-line ceiling.
legacy_width: Optional[int] = None


def set_legacy_width(width: Optional[int]) -> None:
    global legacy_width
    legacy_width = width


def end() -> None:
    """A model on the current schema exists, so stop truncating. Without this
    handover is permanent and the features the bump was made FOR stay
    invisible to the new model."""
    global legacy_width
    legacy_width = None


def truncate(features: list) -> list:
    """Give a handed-over model exactly the leading block it was fitted on."""
    return truncate_for_legacy(features, legacy_width)


def truncate_for_legacy(features: list, legacy_width: Optional[int]) -> list:
    """Give a handed-over model exactly the leading block it was fitted on.

    Never pads a SHORT vector: the neutral for each feature lives in
    `_collect_training_data`, and zero is a real value for several of them.
    """
    if legacy_width and len(features) > legacy_width:
        return features[:legacy_width]
    return features


def hand_over(data_dir) -> tuple:
    """Load the previous version's fitted models so they keep scoring.

    Returns `(batch, online, legacy_width)`. Width is read off the model
    itself, so no table of past feature counts is needed. On any failure it
    returns `(None, None, None)` -- the old discard-and-retrain behaviour,
    which is safe, just gate-open until the first retrain.
    """
    try:
        import joblib
        batch_path = data_dir / "re_ml_batch.pkl"
        online_path = data_dir / "re_ml_online.pkl"
        batch = joblib.load(batch_path) if batch_path.exists() else None
        online = joblib.load(online_path) if online_path.exists() else None
        return batch, online, getattr(batch, "n_features_in_", None)
    except Exception:
        return None, None, None
