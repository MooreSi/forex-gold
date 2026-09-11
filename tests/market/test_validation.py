"""Honest validation for a model whose labels overlap in time.

Section 5.2 of docs/todo/reversal-engine/200. The reversal engine's ML has no
purging, no embargo and no multiple-testing correction. Its labels are path
dependent and overlap: a two-hour pending window, up to six concurrent open
signals, shared level cooldowns. That is exactly the case where plain k-fold
leaks future information into the training set and reports a model that does
not exist out of sample.
"""
from __future__ import annotations

import math

import pytest

from backend.src.services.market import validation as val


class TestPurgedKFold:
    def test_every_sample_is_tested_exactly_once(self):
        folds = val.purged_kfold(n=12, k=3, spans=[(i, i + 1) for i in range(12)])
        tested = [i for _tr, te in folds for i in te]
        assert sorted(tested) == list(range(12))

    def test_the_test_fold_is_contiguous_in_time(self):
        folds = val.purged_kfold(n=12, k=3, spans=[(i, i + 1) for i in range(12)])
        for _tr, te in folds:
            assert te == list(range(te[0], te[-1] + 1))

    def test_a_training_label_overlapping_the_test_window_is_purged(self):
        """The whole point. Sample 3's label resolves at t=8, inside the test
        fold, so training on it means training on the answer."""
        spans = [(i, i + 1) for i in range(12)]
        spans[3] = (3, 8)
        folds = val.purged_kfold(n=12, k=3, spans=spans)
        train_for_middle = next(tr for tr, te in folds if 6 in te)
        assert 3 not in train_for_middle

    def test_the_embargo_drops_samples_that_start_just_after_the_test_fold(self):
        """Serial correlation does not stop at a fold boundary. Without an
        embargo the first samples after the test window are near-copies of
        its last ones."""
        spans = [(i, i + 1) for i in range(12)]
        no_embargo = val.purged_kfold(n=12, k=3, spans=spans, embargo=0.0)
        with_embargo = val.purged_kfold(n=12, k=3, spans=spans, embargo=2.0)
        middle_no = next(tr for tr, te in no_embargo if 6 in te)
        middle_yes = next(tr for tr, te in with_embargo if 6 in te)
        assert len(middle_yes) < len(middle_no)

    def test_a_fold_count_above_the_sample_size_is_refused(self):
        with pytest.raises(ValueError):
            val.purged_kfold(n=3, k=5, spans=[(0, 1)] * 3)


class TestDeflatedSharpe:
    def test_trying_more_configurations_deflates_the_same_sharpe(self):
        """Nine version bumps and twenty retrains are twenty draws from the
        same urn. A Sharpe that is not deflated by how many were tried is a
        number about the search, not the strategy."""
        one = val.deflated_sharpe(0.8, n_obs=250, n_trials=1)
        many = val.deflated_sharpe(0.8, n_obs=250, n_trials=100)
        assert many < one

    def test_a_longer_record_is_more_convincing_when_it_beats_the_benchmark(self):
        """Only ABOVE the selection benchmark. Below it, more observations
        are more evidence of no edge, and a function that returned "more
        convincing" for a losing Sharpe would be wrong in the direction that
        costs money. 2.0 per observation clears the benchmark for 10 trials;
        0.8 does not."""
        assert (val.deflated_sharpe(2.0, n_obs=1000, n_trials=10)
                > val.deflated_sharpe(2.0, n_obs=100, n_trials=10))
        assert (val.deflated_sharpe(0.8, n_obs=1000, n_trials=10)
                < val.deflated_sharpe(0.8, n_obs=100, n_trials=10))

    def test_it_returns_a_probability(self):
        p = val.deflated_sharpe(1.2, n_obs=500, n_trials=20)
        assert 0.0 <= p <= 1.0

    def test_a_zero_sharpe_is_no_better_than_a_coin(self):
        assert val.deflated_sharpe(0.0, n_obs=500, n_trials=1) < 0.6


class TestBacktestOverfitting:
    def test_a_configuration_that_wins_everywhere_gives_a_low_pbo(self):
        # config 0 is genuinely best in every period.
        matrix = [[1.0, 0.1, 0.0] for _ in range(8)]
        assert val.probability_of_backtest_overfitting(matrix, chunks=4) == 0.0

    def test_selecting_the_best_of_twenty_noise_configurations_is_flagged(self):
        """Selecting the best of many configurations on noise is the failure
        this number exists to price, and it comes out HIGH rather than at a
        half. In a finite sample the in-sample and out-of-sample halves
        partition the same fixed set of draws, so a configuration that won
        in sample by luck mechanically gives that luck back on the
        complement. Measured at 0.85 on this fixture; the assertion is the
        direction, not the digit, because the digit depends on the seed."""
        import random
        rng = random.Random(7)
        matrix = [[rng.gauss(0, 1) for _ in range(20)] for _ in range(18)]
        assert val.probability_of_backtest_overfitting(matrix, chunks=6) >= 0.5

    def test_too_few_periods_to_split_is_refused(self):
        with pytest.raises(ValueError):
            val.probability_of_backtest_overfitting([[1.0, 0.0]], chunks=4)


class TestWalkForward:
    def test_each_split_trains_only_on_the_past(self):
        splits = val.walk_forward_splits(n=10, folds=3)
        for train, test in splits:
            assert max(train) < min(test)

    def test_the_training_window_grows(self):
        splits = val.walk_forward_splits(n=12, folds=3)
        assert len(splits[1][0]) > len(splits[0][0])


class TestUniquenessWeights:
    """Overlapping labels are not independent observations, and a fit that
    counts them as if they were is confident about a sample it does not
    have. Up to six reversal signals can be open at once."""

    def test_non_overlapping_samples_all_weigh_the_same(self):
        w = val.uniqueness_weights([(0, 1), (2, 3), (4, 5)])
        assert w == pytest.approx([1.0, 1.0, 1.0])

    def test_a_sample_sharing_its_whole_life_with_another_weighs_half(self):
        w = val.uniqueness_weights([(0, 10), (0, 10)])
        assert w == pytest.approx([0.5, 0.5])

    def test_a_sample_overlapped_for_only_part_of_its_life_weighs_between(self):
        w = val.uniqueness_weights([(0, 10), (5, 10)])
        assert 0.5 < w[0] < 1.0
        assert w[1] == pytest.approx(0.5)

    def test_a_zero_length_span_still_gets_a_weight(self):
        assert val.uniqueness_weights([(5, 5)]) == pytest.approx([1.0])
