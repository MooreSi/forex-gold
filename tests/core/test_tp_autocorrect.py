"""TP autocorrection on a freshly parsed signal.

`_autocorrect_tps` runs on every parsed signal from three parser entry points
plus the AI extractor, and it is about to be lifted into its own module so
`parser.py` can come under the 800-line ceiling. It rewrites the take-profit
ladder of a signal that is on its way to becoming a real order, so it gets
pinned before it moves rather than after.

Three corrections, per its own docstring: drop TPs on the wrong side of the
entry, re-sort out-of-order TPs, and extrapolate the gaps left behind.

Pure function over a dict. Nothing here reaches a broker.
"""
from __future__ import annotations

import pytest

from backend.src.services.signals.parser import _autocorrect_tps


def _raw(**tps):
    row = {f"tp{i}": None for i in range(1, 9)}
    row.update(tps)
    return row


def _ladder(out):
    return [out[f"tp{i}"] for i in range(1, 9) if out.get(f"tp{i}") is not None]


def test_a_clean_buy_ladder_is_left_alone():
    out = _autocorrect_tps("BUY", 2390.0, 2400.0, _raw(tp1=2410.0, tp2=2420.0, tp3=2430.0))
    assert _ladder(out) == [2410.0, 2420.0, 2430.0]


def test_a_clean_sell_ladder_is_left_alone():
    out = _autocorrect_tps("SELL", 2390.0, 2400.0, _raw(tp1=2380.0, tp2=2370.0, tp3=2360.0))
    assert _ladder(out) == [2380.0, 2370.0, 2360.0]


def test_no_take_profits_at_all_is_returned_untouched():
    out = _autocorrect_tps("BUY", 2390.0, 2400.0, _raw())
    assert _ladder(out) == []


def test_a_buy_tp_below_the_entry_is_dropped():
    """A TP the trade would already be past is not a target, it is a typo."""
    out = _autocorrect_tps("BUY", 2390.0, 2400.0, _raw(tp1=2350.0, tp2=2410.0, tp3=2420.0))
    assert 2350.0 not in _ladder(out)


def test_a_sell_tp_above_the_entry_is_dropped():
    out = _autocorrect_tps("SELL", 2390.0, 2400.0, _raw(tp1=2450.0, tp2=2380.0, tp3=2370.0))
    assert 2450.0 not in _ladder(out)


def test_a_buy_ladder_is_measured_from_the_top_of_the_zone():
    """entry_high for a BUY: a TP inside the zone is not a profit target."""
    out = _autocorrect_tps("BUY", 2390.0, 2400.0, _raw(tp1=2395.0, tp2=2410.0))
    assert 2395.0 not in _ladder(out)


def test_a_sell_ladder_is_measured_from_the_bottom_of_the_zone():
    out = _autocorrect_tps("SELL", 2390.0, 2400.0, _raw(tp1=2395.0, tp2=2380.0))
    assert 2395.0 not in _ladder(out)


def test_out_of_order_take_profits_are_sorted_into_the_trade_direction():
    """TP3 nearer than TP1 would bank the ladder in the wrong sequence."""
    out = _autocorrect_tps("BUY", 2390.0, 2400.0, _raw(tp1=2430.0, tp2=2410.0, tp3=2420.0))
    assert _ladder(out) == sorted(_ladder(out))


def test_a_sell_ladder_sorts_downward():
    out = _autocorrect_tps("SELL", 2390.0, 2400.0, _raw(tp1=2360.0, tp2=2380.0, tp3=2370.0))
    assert _ladder(out) == sorted(_ladder(out), reverse=True)


def test_a_signal_with_nothing_salvageable_is_left_for_validation():
    """Every TP on the wrong side: returned as-is on purpose, so
    validate_signal surfaces the error rather than this quietly inventing a
    ladder out of nothing."""
    raw = _raw(tp1=2350.0, tp2=2340.0)
    out = _autocorrect_tps("BUY", 2390.0, 2400.0, raw)
    assert out["tp1"] == 2350.0 and out["tp2"] == 2340.0


def test_the_correction_is_logged_so_a_bad_channel_is_visible(caplog):
    """The docstring promises a WARNING per malformed signal -- that is how a
    channel sending broken ladders gets noticed at all."""
    import logging
    with caplog.at_level(logging.WARNING):
        _autocorrect_tps("BUY", 2390.0, 2400.0, _raw(tp1=2350.0, tp2=2410.0))
    assert any("autocorrect" in r.message.lower() for r in caplog.records), (
        "a silent correction hides the channel that needs fixing"
    )


def test_direction_is_read_case_insensitively():
    """An out-of-order ladder, so BUY and SELL genuinely disagree.

    My first version used an already-ordered ladder, where a lowercase
    "buy" read as SELL produced the same output by coincidence -- the test
    passed against a mutation that removed .upper() entirely. These inputs
    sort ascending for a BUY and are all wrong-side for a SELL, so the two
    readings cannot agree.
    """
    lower = _autocorrect_tps("buy", 2390.0, 2400.0, _raw(tp1=2420.0, tp2=2410.0))
    upper = _autocorrect_tps("BUY", 2390.0, 2400.0, _raw(tp1=2420.0, tp2=2410.0))

    assert _ladder(upper) == [2410.0, 2420.0], "a BUY ladder sorts upward"
    assert _ladder(lower) == _ladder(upper)
