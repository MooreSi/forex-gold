"""`utils/regime.py` holds the frozen blocklists and nothing that looks live.

docs/todo/016 section 1C/1D. `classify_day`, `efficiency_ratio` and
`bounce_pattern_allowed` lost their only caller when the Bounce engine was
deleted (13f03831, 2026-09-14), but the module still described itself as the
shared day-type classifier for both engines. Golden rule 9: dead code that
looks live is deleted, not kept.

The four blocklists are still read (Breakout, and analytics/regime_drift.py),
so their VALUES are pinned here: deleting the dead neighbours must not move a
gate that decides whether a signal exists.

`market/indicators.detect_regime` is NOT pulled out of the Breakout signal
generator even though Breakout never calls it:
`test_the_bounce_engine_backend_is_gone.py` pins that module as the
re-export point for all ten market primitives, and that test is not edited to
fit this change.

Nothing here reaches a broker.
"""
from backend.src.utils import regime


def test_the_dead_classifier_is_gone():
    for name in ("classify_day", "efficiency_ratio", "bounce_pattern_allowed"):
        assert not hasattr(regime, name), name


def test_the_blocklists_keep_their_values():
    assert regime.BOUNCE_BLOCKED_HOURS_UTC == frozenset({7, 12, 13, 14, 15, 16, 20})
    assert regime.BREAKOUT_BLOCKED_HOURS_UTC == frozenset({12, 13, 14})
    assert regime.BOUNCE_BLOCKED_ENTRY_LEVELS == frozenset(
        {"liq_low", "liq_high", "fvg_bull", "fvg_bear"})
    assert regime.BREAKOUT_BLOCKED_LEVELS == frozenset({"fvg_bull", "round"})


def test_the_docstring_no_longer_claims_a_shared_classifier():
    assert "shared day-type" not in (regime.__doc__ or "").lower()
