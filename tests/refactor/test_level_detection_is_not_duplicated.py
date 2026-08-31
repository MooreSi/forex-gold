"""
The Bounce engine and the Reversal engine each detect price levels. A standing
refactor task (docs/todo/refactor/stage1/phase3-expansion-tax/020-consolidate-engine-shared-code.md)
describes level detection as existing "three times (drifted?)" and proposes
moving one "blessed" implementation to a shared home.

These tests exist to say, in code, that **they are not duplicates**. They are
different algorithms, with different windows, different type vocabularies,
different strength semantics and different outputs on identical candles. There
is no common ancestor they drifted apart from, so there is nothing to converge
back to.

That matters because both feed live signal generation:

    identify_key_levels   -> Bounce, Breakout (service + backtest)
    get_all_levels        -> Reversal

Replacing either with the other would change which trades those engines take.
That is a trading-behaviour decision for the owner plus a demo session, not a
tidy-up. If a future change makes these two agree, one of these tests should go
red and the person who made it agree should have to say so out loud.

The third site, `test_signal_velocity._compute_swing_levels`, is covered by
`test_velocity_swings_are_not_pivot_detection` -- it shares the name but not the
job.
"""
import pytest

from backend.src.services.reversal_engine import level_detector as ld
from backend.src.services.test_signal import signal_generator as bounce
from backend.src.services.test_signal import test_signal_velocity as velocity


# A fixed H1 series. Values are literal on purpose -- a generated series would
# make a future failure impossible to reproduce.
_CLOSES = [
    3997.89, 3993.70, 3995.51, 3990.38, 3990.81, 3989.20, 3983.89, 3983.98,
    3978.43, 3977.63, 3972.47, 3967.56, 3966.66, 3970.58, 3966.06, 3962.74,
    3964.27, 3969.64, 3970.57, 3969.33, 3975.04, 3969.60, 3973.90, 3971.38,
    3967.11, 3962.52, 3960.23, 3964.02, 3960.19, 3961.17, 3962.84, 3961.30,
    3961.88, 3956.63, 3951.35, 3947.82, 3949.98, 3949.11, 3946.88, 3947.91,
    3947.35, 3944.94, 3948.48, 3950.87, 3947.79, 3948.69, 3948.99, 3953.49,
    3956.24, 3953.70, 3959.46, 3954.88, 3953.90, 3956.98, 3952.81, 3952.67,
    3947.14, 3949.16, 3952.34, 3953.21,
]

_H1_START_TS = 1735689600  # 2025-01-01 00:00 UTC


@pytest.fixture
def candles() -> list[dict]:
    return [
        {
            "open": px,
            "close": px + 0.5,
            "high": px + 3,
            "low": px - 3,
            "ts": _H1_START_TS + i * 3600,
        }
        for i, px in enumerate(_CLOSES)
    ]


# ── Swing detection ───────────────────────────────────────────────────────────

def test_swing_detection_returns_different_levels_from_the_same_candles(candles):
    """The headline fact: same input, different answer."""
    bounce_prices = {round(lv["price"], 2) for lv in bounce._swing_pivots(candles)}
    reversal_prices = {round(lv["price"], 2) for lv in ld.get_swing_levels(candles)}

    assert bounce_prices, "fixture produced no Bounce pivots -- the test proves nothing"
    assert reversal_prices, "fixture produced no Reversal swings -- the test proves nothing"
    assert bounce_prices != reversal_prices


def test_bounce_scans_every_candle_and_reversal_only_the_recent_window(candles):
    """
    Reversal truncates to its last SWING_LOOKBACK bars before it looks for
    anything, so deleting the older bars cannot change its answer. Bounce walks
    the whole series, so the same deletion does change its answer.
    """
    assert ld.SWING_LOOKBACK < len(candles), "fixture too short to show the truncation"
    recent_only = candles[-ld.SWING_LOOKBACK:]

    def prices(levels):
        return {round(lv["price"], 2) for lv in levels}

    assert prices(ld.get_swing_levels(candles)) == prices(ld.get_swing_levels(recent_only))
    assert prices(bounce._swing_pivots(candles)) != prices(bounce._swing_pivots(recent_only))


def test_the_two_use_different_type_vocabularies(candles):
    bounce_types = {lv["type"] for lv in bounce._swing_pivots(candles)}
    reversal_types = {lv["type"] for lv in ld.get_swing_levels(candles)}

    assert bounce_types == {"support", "resistance"}
    assert reversal_types == {"swing_low", "swing_high"}
    assert not (bounce_types & reversal_types)


def test_strength_means_different_things_in_each():
    """
    Bounce stamps a constant 2. Reversal counts how many neighbouring bars the
    pivot actually beats. A consumer reading `strength` without knowing which
    produced it would be wrong about how significant the level is.

    Uses a series with exactly one clean peak so no de-duplication runs and the
    raw number is visible.
    """
    highs = [4000.0, 4001.0, 4002.0, 4010.0, 4002.5, 4001.5, 4000.5]
    one_peak = [
        {"high": h, "low": h - 50, "open": h, "close": h,
         "ts": _H1_START_TS + i * 3600}
        for i, h in enumerate(highs)
    ]

    (reversal_peak,) = ld.get_swing_levels(one_peak)
    (bounce_peak,) = bounce._swing_pivots(one_peak)

    assert reversal_peak["price"] == bounce_peak["price"] == 4010.0

    # Reversal: one point per neighbouring bar beaten, both sides of the window.
    assert reversal_peak["strength"] == 2 * ld.SWING_WINDOW
    # Bounce: a flat constant, whatever the shape of the peak.
    assert bounce_peak["strength"] == 2


def test_only_reversal_rounds_and_carries_a_candle_index(candles):
    bounce_keys = set(bounce._swing_pivots(candles)[0])
    reversal_keys = set(ld.get_swing_levels(candles)[0])

    assert bounce_keys == {"price", "type", "strength"}
    assert "idx" in reversal_keys
    assert reversal_keys - bounce_keys == {"idx"}


# ── Round-number levels ───────────────────────────────────────────────────────

_PRICE = 4007.0


def test_only_reversal_emits_round_5_midpoints():
    bounce_types = {lv["type"] for lv in bounce._round_number_levels(_PRICE)}
    reversal_types = {lv["type"] for lv in ld.get_round_levels(_PRICE)}

    assert bounce_types == {"round"}
    assert reversal_types == {"round_10", "round_5"}

    bounce_prices = {lv["price"] for lv in bounce._round_number_levels(_PRICE)}
    assert not any(p % 10 == 5 for p in bounce_prices)
    reversal_prices = {lv["price"] for lv in ld.get_round_levels(_PRICE)}
    assert any(p % 10 == 5 for p in reversal_prices)


def test_bounce_filters_round_levels_by_distance_and_reversal_does_not():
    """
    Bounce keeps only levels within its 60-point window; Reversal returns the
    full +/- 6 grid. On the same price they therefore span different ranges.
    """
    bounce_prices = {lv["price"] for lv in bounce._round_number_levels(_PRICE)}
    reversal_10s = {
        lv["price"] for lv in ld.get_round_levels(_PRICE) if lv["type"] == "round_10"
    }

    assert max(abs(p - _PRICE) for p in bounce_prices) <= 60.0
    assert max(abs(p - _PRICE) for p in reversal_10s) > 60.0
    assert reversal_10s - bounce_prices


def test_round_level_strengths_are_on_different_scales():
    bounce_strengths = {lv["strength"] for lv in bounce._round_number_levels(_PRICE)}
    reversal_strengths = {lv["strength"] for lv in ld.get_round_levels(_PRICE)}

    assert bounce_strengths == {1}
    assert reversal_strengths == {2, 3}
    assert not (bounce_strengths & reversal_strengths)


# ── The third site ────────────────────────────────────────────────────────────

def test_velocity_swings_are_not_pivot_detection(candles):
    """
    `_compute_swing_levels` shares a name with the other two and does something
    else entirely: the plain high and low of the last 20 M15 bars, no pivot
    test, no strength, no type. It is a sweep tripwire, not a level list.
    """
    hi, lo = velocity._compute_swing_levels(candles)

    recent = candles[-20:]
    assert hi == max(c["high"] for c in recent)
    assert lo == min(c["low"] for c in recent)

    # It returns a bare pair, not the {price,type,strength} dicts the other two
    # produce -- there is no shape in which these are interchangeable.
    assert isinstance(hi, float) and isinstance(lo, float)


# ── Negative control ──────────────────────────────────────────────────────────

def test_the_comparison_can_detect_agreement(candles):
    """
    Control for the divergence assertions above: comparing an implementation
    with itself must come out equal. Without this, a comparison that always
    reported 'different' would look like a passing suite.
    """
    once = {round(lv["price"], 2) for lv in bounce._swing_pivots(candles)}
    twice = {round(lv["price"], 2) for lv in bounce._swing_pivots(candles)}
    assert once == twice
