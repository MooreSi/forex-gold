"""_classify_ref_level used to hardcode "swing_high" as its fallback
whenever a REF price didn't land within 4pts of the last cycle's top-8
proximity-filtered candidates -- which was most of the time, since those
candidates are filtered to levels near the BOT's own price, not the REF's.
This silently mislabeled the majority of REF-level pattern-learning data.
Now it widens the search to the full unfiltered level set before falling
back to an honest "unknown"."""
from forex_trader.reversal_engine.reversal_engine_correlate import _CorrelationMixin


class _Stub(_CorrelationMixin):
    def __init__(self, cached):
        self._cached = cached


def test_exact_round_number_classified_without_any_cache():
    stub = _Stub({})
    assert stub._classify_ref_level(4000.0) == "round_10"
    assert stub._classify_ref_level(4005.0) == "round_5"


def test_zero_or_negative_price_is_unknown():
    stub = _Stub({})
    assert stub._classify_ref_level(0.0) == "unknown"
    assert stub._classify_ref_level(-5.0) == "unknown"


def test_close_match_in_last_cycle_cache_wins_first():
    stub = _Stub({"levels": [{"price": 4032.0, "type": "asia_low"}]})
    assert stub._classify_ref_level(4033.0) == "asia_low"


def test_falls_back_to_full_level_set_when_not_in_narrow_cache():
    """The narrow 'levels' cache misses (REF price far from bot's own
    proximity window) but the price IS close to a real level in the fuller
    'all_levels' set -- must be classified correctly, not defaulted."""
    stub = _Stub({
        "levels": [{"price": 3900.0, "type": "asia_low"}],
        "all_levels": [
            {"price": 3900.0, "type": "asia_low"},
            {"price": 4041.0, "type": "swing_low"},
        ],
    })
    assert stub._classify_ref_level(4042.5) == "swing_low"


def test_no_nearby_level_anywhere_returns_unknown_not_swing_high():
    """The core regression: previously this always returned 'swing_high'
    no matter how far away the nearest known level actually was."""
    stub = _Stub({
        "levels": [{"price": 3900.0, "type": "asia_low"}],
        "all_levels": [{"price": 3900.0, "type": "asia_low"}],
    })
    assert stub._classify_ref_level(4123.0) == "unknown"


def test_empty_cache_returns_unknown_not_swing_high():
    stub = _Stub({})
    assert stub._classify_ref_level(4123.0) == "unknown"
