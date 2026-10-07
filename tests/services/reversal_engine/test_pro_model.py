"""Pattern inputs from plain dicts; no model fitting or broker access."""
import pytest
from backend.src.services.reversal_engine import pro_model as pm


@pytest.mark.parametrize("value,expected", [(0.0, 0.0), (None, 5.0), (0.3, 0.3)])
def test_pattern_distance_distinguishes_zero_from_missing(value, expected):
    assert pm._vector("BUY", 50, 20, 8, 0, {"fvg_dist_norm": value})[5] == expected
