"""docs/todo/017 item 1: training weights that decay by TIME, keep a floor, and
discount overlapping labels.

What today's weights get wrong, measured 2026-10-14: both engines decay by ROW
position, so a model that sees 100-150 rows a day forgets everything older than
about four days (effective sample size 118 of 5,431). These tests pin the
replacement's properties, each with the control that would fail it.
"""
from __future__ import annotations

import random

import pytest

from backend.src.services.market import sample_weights as sw
from backend.src.services.market import validation as val

DAY = 86400.0


class TestFastUniquenessMatchesTheReference:
    """`uniqueness` must be `validation.uniqueness_weights`, only faster: the
    reference is quadratic in the row count and a refit sees ~5,500 rows."""

    def test_on_random_overlapping_spans(self):
        rng = random.Random(7)
        for _ in range(40):
            spans = []
            for _ in range(rng.randint(1, 30)):
                a = rng.randint(0, 50)
                spans.append((float(a), float(a + rng.choice([0, 0, 1, 3, 7, 20]))))
            assert sw.uniqueness(spans) == pytest.approx(
                val.uniqueness_weights(spans), abs=1e-12)

    def test_empty(self):
        assert sw.uniqueness([]) == []

    def test_two_identical_spans_are_half_a_lone_one(self):
        u = sw.uniqueness([(0.0, 10.0), (0.0, 10.0), (100.0, 110.0)])
        assert u == pytest.approx([0.5, 0.5, 1.0])


class TestDecayIsByTime:
    def test_equal_age_means_equal_decay_however_many_rows_lie_between(self):
        # Row 0 and row 1 are both 10 days old; 50 rows sit after them.
        ages = [10 * DAY, 10 * DAY] + [i * 60.0 for i in range(50)]
        d = sw.decay(ages, half_life_days=14, floor=0.25)
        assert d[0] == d[1]

    def test_positional_weights_fail_that_property(self):
        """The control: today's weights give two equally old rows different
        weights purely because of their row position."""
        n = 52
        positional = [2.718281828 ** (-0.017 * (n - i)) for i in range(n)]
        assert positional[0] != positional[1]

    def test_one_half_life_halves_the_weight(self):
        d = sw.decay([0.0, 14 * DAY], half_life_days=14, floor=0.0)
        assert d[1] == pytest.approx(0.5 * d[0])

    def test_the_floor_holds_for_a_year_old_row(self):
        d = sw.decay([365 * DAY], half_life_days=14, floor=0.25)
        assert d[0] == pytest.approx(0.25)

    def test_without_a_floor_the_same_row_is_negligible(self):
        d = sw.decay([365 * DAY], half_life_days=14, floor=0.0)
        assert d[0] < 1e-7

    def test_bad_parameters_are_refused(self):
        with pytest.raises(ValueError):
            sw.decay([0.0], half_life_days=0, floor=0.25)
        with pytest.raises(ValueError):
            sw.decay([0.0], half_life_days=14, floor=1.5)


class TestTrainingWeights:
    def test_mean_is_one(self):
        spans = [(i * 3600.0, i * 3600.0 + 1800.0) for i in range(20)]
        w = sw.training_weights(spans, half_life_days=14, floor=0.25)
        assert sum(w) / len(w) == pytest.approx(1.0)

    def test_age_is_measured_from_the_newest_label_not_the_clock(self):
        """Deterministic: the same rows give the same weights whenever fitted."""
        spans = [(0.0, 100.0), (5 * DAY, 5 * DAY + 100.0)]
        assert sw.training_weights(spans, 14, 0.25) == sw.training_weights(
            [(a + 1e6, b + 1e6) for a, b in spans], 14, 0.25)

    def test_an_old_regime_keeps_real_weight(self):
        """The point of the change: 30-day-old rows still count for something
        next to 600 fresh ones. Under the positional formula they are ~0."""
        old = [(i * 60.0, i * 60.0 + 30.0) for i in range(50)]
        new = [(30 * DAY + i * 60.0, 30 * DAY + i * 60.0 + 30.0) for i in range(600)]
        w = sw.training_weights(old + new, half_life_days=14, floor=0.25)
        share_old = sum(w[:50]) / sum(w)
        assert share_old > 0.01
        n = 650
        pos = [2.718281828 ** (-0.017 * (n - i)) for i in range(n)]
        assert sum(pos[:50]) / sum(pos) < 1e-4

    def test_overlap_is_discounted(self):
        spans = [(0.0, 10.0), (0.0, 10.0), (20.0, 30.0)]
        w = sw.training_weights(spans, half_life_days=14, floor=0.25)
        assert w[0] == pytest.approx(w[2] / 2, rel=1e-4)  # 20 s of age apart

    def test_effective_sample_size(self):
        assert sw.effective_size([1.0] * 10) == pytest.approx(10.0)
        assert sw.effective_size([1.0] + [0.0] * 9) == pytest.approx(1.0)
        assert sw.effective_size([]) == 0.0

    def test_empty(self):
        assert sw.training_weights([], 14, 0.25) == []
