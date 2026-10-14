"""docs/todo/017 step 1b: the walk-forward replay must never let a test row's
prediction see a label that closed after the row was created, and its
statistics must be what they say.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import weight_replay as wr


class _Mean:
    def __init__(self, y):
        self.v = sum(y) / len(y)

    def predict(self, X):
        return [self.v] * len(X)


def _rows(n, gap=3600.0, hold=1800.0):
    return [{"created_at": i * gap, "close_time": i * gap + hold,
             "x": [float(i)], "y": float(i)} for i in range(n)]


class TestNoLookAhead:
    def test_each_block_trains_only_on_labels_closed_before_it(self):
        seen = []

        def fit(X, y, w):
            seen.append(max(v[0] for v in X))
            return _Mean(y)
        rows = _rows(100)
        wr.replay(rows, lambda t: None, fit, n_test=40, block=10, min_train=5)
        firsts = [60, 70, 80, 90]
        assert seen == [f - 1 for f in firsts]

    def test_a_label_still_open_at_the_block_start_is_excluded(self):
        """Hold longer than the gap: the previous signal has not closed yet."""
        seen = []

        def fit(X, y, w):
            seen.append(max(v[0] for v in X))
            return _Mean(y)
        wr.replay(_rows(100, hold=5400.0), lambda t: None, fit,
                  n_test=10, block=10, min_train=5)
        assert seen == [88]      # row 89 closes at 90.5h, after the block opens at 90h

    def test_weights_are_computed_on_the_training_rows(self):
        got = []

        def weights(train):
            got.append(len(train))
            return [1.0] * len(train)
        wr.replay(_rows(60), weights, lambda X, y, w: _Mean(y), n_test=20, block=10,
                  min_train=5)
        assert got == [40, 50]


class TestStatistics:
    def test_spearman(self):
        assert wr.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
        assert wr.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
        assert wr.spearman([1, 2], [1, 2]) is None

    def test_lift(self):
        assert wr.lift([1, -1, 1, -1], [1.0, -1.0, 1.0, -1.0]) == pytest.approx(1.0)
        assert wr.lift([-1, -1], [1.0, 2.0]) is None

    def test_bootstrap_interval_contains_the_point_on_clean_data(self):
        ts = [i * 7200.0 for i in range(50)]
        y = [1.0 + (0.1 if i % 2 else -0.1) for i in range(50)]
        lo, hi = wr.block_bootstrap(ts, lambda idx: sum(y[i] for i in idx) / len(idx), n=500)
        assert lo < 1.0 < hi and hi - lo < 0.2
