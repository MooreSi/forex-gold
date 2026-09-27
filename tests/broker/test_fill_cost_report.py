"""The fill-cost report the Dashboard shows: what fills really cost, measured.

Reads `execution_quality` (written by broker/tca.py) and summarises it. Two
rules carried over from tca.py: an unmeasured fill is not a free one (it is
counted separately, never averaged in as zero), and slippage is signed, so
the share of fills that went AGAINST us is reported, not just the average.

Nothing here reaches a broker: rows are fed straight to the function.
"""
from __future__ import annotations

import time

from backend.src.services.broker import fill_cost_report as report

NOW = 1_790_000_000.0


def row(cost, slip, spread=0.2, strategy="template:A", measured=1, cost_r=0.2, age_h=1):
    return {"open_time": NOW - age_h * 3600, "cost_pts": cost, "slippage_pts": slip,
            "spread_cost_pts": spread, "strategy": strategy, "measured": measured,
            "cost_r": cost_r}


def test_the_headline_figures():
    rows = [row(0.5, 0.3), row(1.0, 0.8), row(1.5, -0.2), row(2.0, 1.0)]
    out = report.summarise(rows, days=14, now=NOW)
    assert out["n"] == 4
    assert out["median_cost_pts"] == 1.25
    assert out["median_slippage_pts"] == 0.55
    assert out["adverse_share"] == 0.75
    assert out["favourable_share"] == 0.25
    assert out["median_spread_pts"] == 0.2


def test_unmeasured_fills_are_counted_not_averaged_as_free():
    rows = [row(1.0, 0.5), row(None, None, measured=0)]
    out = report.summarise(rows, days=14, now=NOW)
    assert out["n"] == 1
    assert out["unmeasured"] == 1
    assert out["median_cost_pts"] == 1.0


def test_only_the_window_counts():
    rows = [row(1.0, 0.5), row(9.0, 9.0, age_h=24 * 20)]
    out = report.summarise(rows, days=14, now=NOW)
    assert out["n"] == 1


def test_nothing_measured_is_none_not_zero():
    out = report.summarise([], days=14, now=NOW)
    assert out["n"] == 0
    assert out["median_cost_pts"] is None
    assert out["adverse_share"] is None


def test_split_by_strategy_most_used_first():
    rows = [row(1.0, 0.5, strategy="B")] + [row(0.4, 0.1, strategy="A")] * 3
    out = report.summarise(rows, days=14, now=NOW)
    assert [s["strategy"] for s in out["by_strategy"]] == ["A", "B"]
    assert out["by_strategy"][0]["n"] == 3
    assert out["by_strategy"][0]["median_cost_pts"] == 0.4


def test_p90_and_cost_as_a_share_of_the_stop():
    rows = [row(float(i), 0.1, cost_r=i / 10) for i in range(1, 11)]
    out = report.summarise(rows, days=14, now=NOW)
    assert out["p90_cost_pts"] == 9.0
    assert out["median_cost_r"] == 0.55
