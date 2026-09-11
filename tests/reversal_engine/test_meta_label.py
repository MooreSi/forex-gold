"""The meta-labeller: act or do not act, as a separate question.

Section 1.3 and 5.2 of docs/todo/reversal-engine/200.

`re_ml` regresses R-multiple over the whole signal population and blocks
below zero. `re_ml_meta.pkl` records mean_r = -0.083 at every retrain, so as
the model gets better it blocks more: 31% of signals executed on 2026-09-02,
11% on 2026-09-07. A gate converging on "trade nothing" is not a filter, it
is a verdict on the generator.

The professional shape is to split the question. The level detector keeps
deciding DIRECTION. A second, small, honestly validated classifier decides
only whether to act, and its label is binary -- did this trade clear its
cost -- which is learnable on 4,800 rows in a way a continuous R regression
is not.

The refusals are the important part. A gate that arms itself on a model that
cannot beat a coin is worse than no gate, because it is trusted.
"""
from __future__ import annotations

import random

import pytest

from backend.src.services.reversal_engine import meta_label as ml


def rows_with_signal(n=400, seed=3):
    """A learnable population: feature 0 genuinely predicts the outcome."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        good = rng.random() < 0.5
        x = rng.gauss(1.0 if good else -1.0, 0.7)
        out.append({"features": [x, rng.gauss(0, 1)],
                    "realised_r": 1.5 if good else -1.0,
                    "cost_r": 0.1, "open_time": i * 100.0,
                    "close_time": i * 100.0 + 50.0})
    return out


def rows_of_noise(n=400, seed=5):
    rng = random.Random(seed)
    return [{"features": [rng.gauss(0, 1), rng.gauss(0, 1)],
             "realised_r": rng.choice([1.5, -1.0]), "cost_r": 0.1,
             "open_time": i * 100.0, "close_time": i * 100.0 + 50.0}
            for i in range(n)]


class TestTheLabel:
    def test_a_trade_that_cleared_its_cost_is_a_one(self):
        assert ml.label_for(realised_r=0.5, cost_r=0.2) == 1

    def test_a_trade_that_did_not_cover_its_cost_is_a_zero(self):
        """Not "did it win". A +0.05R win that cost 0.18R to execute is a
        trade the system should not have taken, and labelling it a success
        teaches the gate to keep taking it."""
        assert ml.label_for(realised_r=0.05, cost_r=0.18) == 0

    def test_an_unmeasured_cost_is_not_treated_as_free(self):
        assert ml.label_for(realised_r=0.05, cost_r=None) is None


class TestFitting:
    def test_it_refuses_below_the_minimum_sample(self):
        m = ml.MetaLabeller(min_samples=200)
        status = m.fit(rows_with_signal(n=50))
        assert status.ready is False
        assert "sample" in status.refusal.lower()

    def test_it_refuses_a_model_that_cannot_beat_a_coin_out_of_sample(self):
        """pro_model already holds this line: 0.5 whenever the model cannot
        beat a coin out of sample, because an uninformative model and a
        missing one must read identically."""
        m = ml.MetaLabeller(min_samples=100, min_auc=0.55)
        status = m.fit(rows_of_noise())
        assert status.ready is False
        assert status.auc_oos is not None
        assert status.auc_oos < 0.65

    def test_it_arms_on_a_genuinely_learnable_population(self):
        m = ml.MetaLabeller(min_samples=100, min_auc=0.55)
        status = m.fit(rows_with_signal())
        assert status.ready is True
        assert status.auc_oos > 0.7

    def test_the_reported_auc_is_out_of_sample_not_in_sample(self):
        """Fitted on purged, embargoed folds. An in-sample AUC on overlapping
        labels is the number that makes a leaking model look excellent."""
        m = ml.MetaLabeller(min_samples=100)
        status = m.fit(rows_with_signal())
        assert status.n_folds >= 2
        assert status.auc_in_sample >= status.auc_oos - 0.35


class TestUsingIt:
    def test_an_unfitted_model_has_no_opinion(self):
        assert ml.MetaLabeller().probability([0.0, 0.0]) is None

    def test_a_refused_model_still_has_no_opinion(self):
        m = ml.MetaLabeller(min_samples=100, min_auc=0.99)
        m.fit(rows_with_signal())
        assert m.probability([1.0, 0.0]) is None

    def test_an_armed_model_scores_a_good_setup_above_a_bad_one(self):
        m = ml.MetaLabeller(min_samples=100, min_auc=0.55)
        m.fit(rows_with_signal())
        assert m.probability([2.0, 0.0]) > m.probability([-2.0, 0.0])

    def test_a_model_with_no_opinion_blocks_nothing(self):
        """Not armed is not the same as "block everything". The gate is off
        by default and an unfitted model must leave behaviour exactly as it
        is -- which is also why the caller checks `status.ready` rather than
        relying on this."""
        assert ml.MetaLabeller().should_take([0.0, 0.0], threshold=0.9) is True

    def test_an_armed_model_blocks_below_its_threshold(self):
        m = ml.MetaLabeller(min_samples=100, min_auc=0.55)
        m.fit(rows_with_signal())
        assert m.should_take([-3.0, 0.0], threshold=0.9) is False
        assert m.should_take([3.0, 0.0], threshold=0.1) is True
