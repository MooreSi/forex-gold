"""`get_regime_score` is an ADX bucket, and says so.

docs/todo/016 section 1B. Until 2026-10-11 the function took `atr` and
`atr_avg` and promised to promote a volatile market to 0.5 when
`atr / atr_avg > 1.5`. All six production call sites passed `(adx, atr)`, so
`atr_avg` was always 0.0 and that branch never ran: the ML feature
`regime_score` has only ever been an ADX bucket. The branch is deleted;
volatility reaches the model through its own switch-gated feature instead
(`vol_regime`).

The live value must not move, because 7,553 stored training vectors carry it.
`_OLD` below is the function as it was, so the grid compares against the code
that wrote those vectors rather than against a restatement of it.

Nothing here reaches a broker.
"""
import inspect

from backend.src.services.analytics import read_repo


def _OLD(adx: float, atr: float, atr_avg: float = 0.0) -> float:
    if adx >= 40:
        regime = 1.0
    elif adx >= 28:
        regime = 0.75
    elif adx >= 20:
        regime = 0.5
    elif adx >= 14:
        regime = 0.25
    else:
        regime = 0.0
    if atr_avg > 0 and atr / atr_avg > 1.5:
        regime = max(regime, 0.5)
    return regime


def test_atr_avg_is_gone_and_atr_is_kept_for_its_callers():
    """`atr` stays in the signature only so six live call sites (and the
    test double in test_process_candidate_suppressors.py) keep their shape;
    it is not read. `atr_avg` existed for the dead branch and is gone."""
    params = list(inspect.signature(read_repo.get_regime_score).parameters)
    assert params == ["adx", "atr"], params


def test_every_value_production_ever_computed_is_unchanged():
    """Production never passed atr_avg, so the old function's output with
    atr_avg=0 is exactly what every stored vector holds."""
    for adx10 in range(0, 801):
        adx = adx10 / 10.0
        for atr in (0.5, 1.0, 5.0, 8.0, 20.0, 60.0, 200.0):
            assert read_repo.get_regime_score(adx, atr) == _OLD(adx, atr), (adx, atr)


def test_the_grid_can_tell_a_changed_bucket_apart():
    """Negative control: the old function with a real atr_avg disagrees with
    the ADX-only bucket on a quiet-ADX, high-ATR input, so the grid above is
    capable of seeing a volatility promotion if one came back."""
    assert _OLD(10.0, 30.0, atr_avg=10.0) == 0.5
    assert read_repo.get_regime_score(10.0, 30.0) == 0.0


def test_the_database_facade_still_exports_it():
    from backend.src.db import database
    assert database.get_regime_score(45.0, 8.0) == 1.0
