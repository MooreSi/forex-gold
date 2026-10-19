"""The offline replay must expose exactly the OHLC known at a decision.
No runtime or broker calls are reachable from these candle-only helpers.
"""
from tools.setforget_replay import asof


def test_a_partial_h4_uses_no_future_extreme_or_close():
    future = [{"ts": 0, "open": 2000, "high": 9999, "low": 1, "close": 9999}]
    known = [{"ts": 0, "open": 2000, "high": 2002, "low": 1998, "close": 2001}]
    assert asof(future, [0], 1800, 14400, known, 400) == known


def test_at_an_h4_boundary_the_live_series_includes_a_new_forming_bar():
    bars = [{"ts": 0, "open": 2000, "high": 2002, "low": 1998, "close": 2001}]
    visible = asof(bars, [0], 14400, 14400, bars, 400)
    assert visible == bars + [{"ts": 14400, "open": 2001, "high": 2001,
                               "low": 2001, "close": 2001}]
