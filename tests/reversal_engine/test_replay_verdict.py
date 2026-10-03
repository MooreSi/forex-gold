"""The pre-registered bar for the Dukascopy replay (docs/todo/reversal-engine/260).

The bar was written before any result existed: z >= 3 against the placebo
with 2-hour clustering, n >= 200, mean R after costs positive in BOTH halves
and in more than two thirds of the calendar years that have 200 signals. A
slice must reach z >= 4. These tests pin that the code cannot pass something
the document would fail, and that each condition can fail on its own.
"""
import random
from datetime import datetime, timezone

from backend.src.services.reversal_engine import replay_verdict as rv


def _ts(year, i):
    return datetime(year, 1, 2, tzinfo=timezone.utc).timestamp() + i * 7200.0


def _rows(years, per_year, win_p, placebo=0.5, r_win=1.0, r_loss=-1.0, seed=3, key="b55",
          year_win_p=None):
    rng = random.Random(seed)
    out = []
    for y in years:
        p = (year_win_p or {}).get(y, win_p)
        for i in range(per_year):
            won = rng.random() < p
            ts = _ts(y, i)
            out.append({"ts": ts, "cluster": int(ts // 7200), f"{key}_won": won,
                        f"{key}_p": placebo, f"{key}_r": r_win if won else r_loss})
    return out


class TestTheBar:
    def test_a_real_edge_in_every_year_passes(self):
        j = rv.judge(_rows([2020, 2021, 2022, 2023], 300, 0.62, placebo=0.5), "b55")
        assert j.passes, j.failures
        assert j.failures == []

    def test_no_edge_fails_on_z(self):
        j = rv.judge(_rows([2020, 2021, 2022, 2023], 300, 0.5, placebo=0.5), "b55")
        assert not j.passes
        assert any("z" in f for f in j.failures)

    def test_too_few_signals_fails_even_with_a_big_z(self):
        j = rv.judge(_rows([2021], 120, 0.9, placebo=0.5), "b55")
        assert not j.passes
        assert any("n" in f for f in j.failures)

    def test_an_edge_that_lives_in_one_half_only_fails(self):
        rows = _rows([2020, 2021, 2022, 2023], 300, 0.8, placebo=0.5)
        late = [r for r in rows if r["ts"] > _ts(2022, 0)]
        for r in late:
            r["b55_r"] = -0.3
        j = rv.judge(rows, "b55")
        assert not j.passes
        assert any("half" in f for f in j.failures)

    def test_an_edge_in_a_minority_of_years_fails(self):
        # Wins well above the placebo in 2020 only; the other years pay -1R
        # on every trade yet keep the overall z high through 2020's size.
        rows = _rows([2020, 2021, 2022, 2023], 300, 0.5, placebo=0.5,
                     year_win_p={2020: 0.95})
        for r in rows:
            if r["ts"] > _ts(2021, 0):
                r["b55_r"] = -0.2
        j = rv.judge(rows, "b55")
        assert not j.passes
        assert any("year" in f for f in j.failures)

    def test_years_with_fewer_than_200_signals_do_not_count_for_or_against(self):
        rows = _rows([2020, 2021, 2022, 2023], 300, 0.62, placebo=0.5)
        tiny = _rows([2019], 20, 0.62, placebo=0.5)
        for r in tiny:
            r["b55_r"] = -5.0
        j = rv.judge(rows + tiny, "b55")
        assert 2019 not in j.counted_years
        assert j.passes, j.failures

    def test_nothing_resolved_is_not_a_pass(self):
        j = rv.judge([], "b55")
        assert not j.passes and j.summary is None


class TestSlices:
    def test_a_slice_needs_z_four_not_three(self):
        assert not rv.slice_is_signal({"z": 3.9, "n": 500})
        assert rv.slice_is_signal({"z": 4.0, "n": 500})

    def test_a_slice_with_too_few_rows_is_never_a_signal(self):
        assert not rv.slice_is_signal({"z": 9.0, "n": 50})

    def test_a_slice_with_no_z_is_not_a_signal(self):
        assert not rv.slice_is_signal({"z": None, "n": 500})
