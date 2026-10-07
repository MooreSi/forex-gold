"""Corrupt labels/vectors are rejected before model training; no broker."""
import json
import pytest
from backend.src.services.reversal_engine.ml_engine import _training_data as td


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_profit_has_no_trainable_label(value):
    assert td._realised_r({"sl_dist": 5, "net_pnl_dollars": value}) is None


def test_nonfinite_feature_is_not_read_as_a_trainable_vector():
    assert td.stored_vector({"ml_features_json": json.dumps([float("nan")]*38)}) is None
