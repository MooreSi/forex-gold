"""When the Breakout ML version changes, hand the old model over -- do not bin it.

`MODEL_VERSION` is `N_FEATURES` and the file names embed it, so the first start
after a feature is appended finds nothing for the new version. The previous
model keeps scoring instead, on the leading features it was fitted on. Valid
because features are append-only (`ml_engine._pad_legacy`'s contract): the
first N of a new vector ARE the old vector.

The mirror of `reversal_engine.ml_handover`, with two differences. Breakout's
file names carry the version, so the previous files are found by scanning the
data directory rather than by a fixed name. And its label has been the same
R-multiple (`rr_tp1` / -1 / 0) throughout, so there is no label epoch to
refuse across; the floor is `MIN_HANDOVER_VERSION`, the shortest vector
`_pad_legacy` will replay.

State lives here, not in `ml_engine`, which every caller imports.
"""
from __future__ import annotations

import re
from typing import Optional

# Same floor as ml_engine._pad_legacy: below it the stored history cannot be
# replayed against the model either.
MIN_HANDOVER_VERSION = 15

_BATCH = re.compile(r"^bo_ml_(batch|online)_v(\d+)\.joblib$")

# Width the handed-over models were fitted on; None means the loaded models
# match the current schema.
legacy_width: Optional[int] = None


def set_legacy_width(width: Optional[int]) -> None:
    global legacy_width
    legacy_width = width


def end() -> None:
    """A model on the current schema exists, so stop truncating."""
    set_legacy_width(None)


def truncate(features: list) -> list:
    """Give a handed-over model exactly the leading block it was fitted on.
    Never pads a short vector."""
    if legacy_width and len(features) > legacy_width:
        return features[:legacy_width]
    return features


def model_width(model) -> Optional[int]:
    """Feature count a fitted model expects. sklearn estimators and pipelines
    carry `n_features_in_`; a LightGBM Booster (what `lgb.train` returns) has
    `num_feature()` instead."""
    if model is None:
        return None
    n = getattr(model, "n_features_in_", None)
    if n is None and hasattr(model, "num_feature"):
        try:
            n = model.num_feature()
        except Exception:
            n = None
    return int(n) if n else None


def _previous_version(data_dir, current_version: int) -> Optional[int]:
    best = None
    try:
        names = [p.name for p in data_dir.iterdir()]
    except OSError:
        return None
    for name in names:
        m = _BATCH.match(name)
        if not m:
            continue
        v = int(m.group(2))
        if MIN_HANDOVER_VERSION <= v < current_version and (best is None or v > best):
            best = v
    return best


def hand_over(data_dir, current_version: int) -> tuple:
    """Load the highest older version's fitted models so they keep scoring.

    Returns `(batch, online, width)`. Any failure returns `(None, None, None)`,
    the old behaviour (start fresh, retrain at init), which is safe.
    """
    try:
        import joblib
        v = _previous_version(data_dir, current_version)
        if v is None:
            return None, None, None
        bp = data_dir / f"bo_ml_batch_v{v}.joblib"
        op = data_dir / f"bo_ml_online_v{v}.joblib"
        batch = joblib.load(bp) if bp.exists() else None
        online = joblib.load(op) if op.exists() else None
        # Same guard _load_all applies: an SGDClassifier pipeline predicts a
        # different quantity.
        reg = online.named_steps.get("reg") if hasattr(online, "named_steps") else None
        if online is not None and (reg is None or not hasattr(reg, "t_")):
            online = None
        width = model_width(batch) or model_width(online) or v
        if online is not None and model_width(online) not in (None, width):
            online = None
        if batch is None and online is None:
            return None, None, None
        return batch, online, width
    except Exception:
        return None, None, None
