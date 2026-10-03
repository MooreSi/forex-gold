"""The pro model's AUC must not be earned by knowing the date (2026-10-02).

Measured on the live corpus: 0.82 with the random stratified folds the model
used, 0.68 with whole days held out, 0.57 trained on the first 60% of time and
scored on the last 40%. Background snapshots are taken every 15 minutes, so
random folds put a snapshot's neighbours on both sides of the split and the
forest partly learns the day (RSI, ATR and price level drift). Its output,
meanwhile, ranks our own trades' outcomes at AUC 0.496.

The gate (`_MIN_AUC`) is unchanged. What changed is the number it is applied
to, and a second number reported beside it.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import pro_model
from tests.reversal_engine.test_pro_model import (  # noqa: F401  (fixture)
    corpus, seed_separable, _row)


class TestBlockedFolds:

    def test_folds_are_contiguous_blocks_in_time_order(self):
        times = [5, 1, 9, 3, 7, 2, 8, 4, 6, 0, 11, 10]
        folds = pro_model._blocked_folds(times, 4)
        assert len(folds) == 4
        covered = sorted(i for f in folds for i in f)
        assert covered == list(range(len(times)))
        # Each fold's times all precede the next fold's times.
        spans = [(min(times[i] for i in f), max(times[i] for i in f)) for f in folds]
        assert all(spans[k][1] < spans[k + 1][0] for k in range(3))

    def test_ties_in_time_still_split_into_the_requested_number_of_folds(self):
        folds = pro_model._blocked_folds([1.0] * 12, 4)
        assert sorted(len(f) for f in folds) == [3, 3, 3, 3]

    def test_fewer_rows_than_folds_gives_fewer_folds_not_an_error(self):
        assert len(pro_model._blocked_folds([1.0, 2.0], 4)) == 2


class TestTheCorpus:

    def test_a_signal_captured_at_several_stages_is_one_example(self, corpus):
        for stage in ("market_call", "levels", "complete"):
            corpus.insert(_row("msg-1", stage, rsi=33, adx=31))
        X, y, w, _ = pro_model._dataset()
        assert sum(y) == 1

    def test_rows_come_back_oldest_first_with_their_message_id(self, corpus):
        corpus.insert(_row("a", "complete", rsi=33, adx=31))
        got = corpus.rows(background=False)
        assert got[0]["tg_message_id"] == "a"


class TestWhatIsReported:

    def test_the_reported_auc_comes_from_blocked_folds(self, corpus, monkeypatch):
        seen = {}
        real = pro_model._blocked_folds

        def spy(times, n):
            seen["called"] = True
            return real(times, n)

        monkeypatch.setattr(pro_model, "_blocked_folds", spy)
        seed_separable(corpus)
        pro_model.fit(force=True)
        assert seen.get("called")

    def test_a_forward_in_time_auc_is_reported_beside_it(self, corpus):
        seed_separable(corpus)
        s = pro_model.fit(force=True)
        assert "auc_forward" in s
        assert s["auc_forward"] is None or 0.0 <= s["auc_forward"] <= 1.0
