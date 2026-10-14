"""docs/todo/017 item 4: expectancy by market-state cell, with an interval
that does not pretend overlapping trades are independent, and both halves of
the sample shown (014's test: a cell must hold in both).
"""
from __future__ import annotations

import math

import pytest

from backend.src.services.analytics import regime_table as rt

H = 3600.0


def _r(ts, r, regime="range", side="flat", vol="normal", session="london"):
    return {"ts": ts, "r": r, "state": {"h1_regime": regime, "h1_side": side,
                                        "vol_state": vol, "session": session}}


class TestCellStats:
    def test_mean_and_count(self):
        rows = [_r(i * 3 * H, v) for i, v in enumerate([1.0, -1.0, 0.5, 0.5])]
        c = rt.cell_stats(rows)
        assert c["n"] == 4 and c["mean_r"] == pytest.approx(0.25)

    def test_trades_in_one_block_are_one_cluster(self):
        """Ten trades inside one 2-hour block carry one observation's worth of
        information. Treated as independent, the interval would shrink by
        about sqrt(10); clustered, it cannot be computed from one block."""
        rows = [_r(60.0 * i, 1.0 if i % 2 else -0.5) for i in range(10)]
        c = rt.cell_stats(rows)
        assert c["clusters"] == 1 and c["se"] is None

    def test_clustered_se_is_wider_than_the_naive_one_when_blocks_agree(self):
        rows = []
        for b in range(20):
            v = 1.0 if b % 2 else -1.0
            rows += [_r(b * 2 * H + 60.0 * k, v) for k in range(5)]
        c = rt.cell_stats(rows)
        naive = math.sqrt(sum((x["r"] - c["mean_r"]) ** 2 for x in rows) / (len(rows) - 1)) / math.sqrt(len(rows))
        assert c["se"] > naive * 1.5

    def test_halves_are_split_by_time(self):
        rows = [_r(i * 3 * H, -1.0) for i in range(10)] + \
               [_r((10 + i) * 3 * H, 1.0) for i in range(10)]
        c = rt.cell_stats(rows, split_ts=10 * 3 * H)
        assert (c["first_half"], c["second_half"]) == (pytest.approx(-1.0), pytest.approx(1.0))
        assert c["holds"] is False

    def test_a_cell_holds_only_with_both_halves_and_the_interval_above_zero(self):
        rows = [_r(i * 3 * H, 1.0 + (0.1 if i % 2 else -0.1)) for i in range(40)]
        c = rt.cell_stats(rows, split_ts=20 * 3 * H)
        assert c["holds"] is True

    def test_empty(self):
        assert rt.cell_stats([])["n"] == 0


class TestTheTable:
    def test_groups_by_each_axis_and_the_regime_by_volatility_cross(self):
        rows = [_r(0, 1.0, regime="trend", side="with", vol="high"),
                _r(3 * H, -1.0, regime="range", vol="low"),
                _r(6 * H, 0.5, regime="trend", side="against", vol="high")]
        t = rt.table(rows)
        assert set(t["regime"]) == {"trend_with", "trend_against", "range"}
        assert set(t["vol_state"]) == {"high", "low"}
        assert t["regime x vol"]["trend_with | high"]["n"] == 1

    def test_unmeasured_rows_are_counted_apart_not_dropped_silently(self):
        rows = [_r(0, 1.0), {"ts": H, "r": -1.0, "state": None},
                {"ts": 2 * H, "r": None, "state": {"h1_regime": "range"}}]
        t = rt.table(rows)
        assert t["unmeasured"] == 1 and t["unlabelled"] == 1
        assert t["regime"]["range"]["n"] == 1
