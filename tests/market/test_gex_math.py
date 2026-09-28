"""Gamma exposure arithmetic for the GEX snapshot collector (docs/todo/009).

Pure functions over an option chain. The sign convention is the common
"naive GEX" one -- dealers long calls, short puts, so call gamma counts
positive and put gamma negative -- and it is an ASSUMPTION about who holds
what, which is why the collector also stores the raw chain: a later study
can recompute under a different one.

Nothing here reaches a broker or the network.
"""
from __future__ import annotations

import math

import pytest

from backend.src.services.market import gex


def test_black_scholes_gamma_matches_a_known_value():
    # S=K=100, 1y, 20% vol, r=0: gamma = pdf(0.1) / (100 * 0.2) = 0.019848
    assert gex.bs_gamma(100.0, 100.0, 1.0, 0.2, r=0.0) == pytest.approx(0.019848, abs=1e-5)


def test_gamma_is_zero_for_unusable_inputs():
    assert gex.bs_gamma(100.0, 100.0, 0.0, 0.2) == 0.0
    assert gex.bs_gamma(100.0, 100.0, 1.0, 0.0) == 0.0
    assert gex.bs_gamma(100.0, 100.0, 1.0, float("nan")) == 0.0


def _row(strike, call_oi=0, put_oi=0, iv=0.2, t=30 / 365):
    return {"strike": strike, "t_years": t, "call_oi": call_oi, "put_oi": put_oi,
            "call_iv": iv, "put_iv": iv}


def test_calls_count_positive_and_puts_negative():
    rows = [_row(100, call_oi=1000), _row(100, put_oi=1000)]
    per = gex.per_strike(rows, spot=100.0)
    assert per[0]["call_gex"] > 0 and per[0]["put_gex"] == 0
    assert per[1]["put_gex"] < 0 and per[1]["call_gex"] == 0
    assert per[0]["call_gex"] == pytest.approx(-per[1]["put_gex"])


def test_dollar_gamma_per_one_percent_move():
    """gamma * OI * 100 shares * S^2 * 1%."""
    rows = [_row(100, call_oi=10, t=1.0)]
    g = gex.bs_gamma(100.0, 100.0, 1.0, 0.2)
    assert gex.per_strike(rows, spot=100.0)[0]["call_gex"] == pytest.approx(g * 10 * 100 * 100 ** 2 * 0.01)


def test_a_row_without_implied_volatility_contributes_nothing():
    rows = [_row(100, call_oi=1000, iv=0.0), _row(100, call_oi=1000, iv=float("nan"))]
    assert gex.total_gex(rows, spot=100.0) == 0.0


def test_walls_are_the_heaviest_call_and_put_strikes():
    rows = [_row(95, put_oi=5000), _row(98, put_oi=1000),
            _row(102, call_oi=1000), _row(105, call_oi=6000)]
    s = gex.summarise(rows, spot=100.0)
    assert s["call_wall"] == 105
    assert s["put_wall"] == 95


def test_the_flip_is_where_total_gex_changes_sign():
    """Puts below, calls above: below the flip puts dominate (negative), above
    it calls do (positive), so the flip sits between them."""
    rows = [_row(95, put_oi=4000), _row(105, call_oi=4000)]
    s = gex.summarise(rows, spot=100.0)
    assert s["flip_level"] is not None
    assert 95 < s["flip_level"] < 105
    assert gex.total_gex(rows, spot=s["flip_level"] - 1) < 0 < gex.total_gex(rows, spot=s["flip_level"] + 1)


def test_no_sign_change_in_range_means_no_flip():
    rows = [_row(100, call_oi=1000)]
    assert gex.summarise(rows, spot=100.0)["flip_level"] is None


def test_an_empty_chain_summarises_to_nothing_not_zero():
    s = gex.summarise([], spot=100.0)
    assert s == {"total_gex": None, "flip_level": None, "call_wall": None,
                 "put_wall": None, "n_rows": 0}


def test_levels_convert_to_gold_by_the_snapshot_ratio():
    assert gex.to_xau(400.0, ratio=10.8) == pytest.approx(4320.0)
    assert gex.to_xau(None, ratio=10.8) is None
    assert gex.to_xau(400.0, ratio=None) is None


def test_with_two_crossings_the_flip_is_the_one_nearest_spot():
    """Puts at 88 and 112 around calls at 97: total GEX changes sign near 93
    and near 103. The flip that matters for today is the one price can reach
    first, the nearer."""
    def row(k, c=0, p=0):
        return {"strike": k, "t_years": 30 / 365, "call_oi": c, "put_oi": p,
                "call_iv": 0.15, "put_iv": 0.15}
    rows = [row(88, p=6000), row(97, c=3000), row(112, p=6000)]
    flip = gex.summarise(rows, spot=100.0)["flip_level"]
    assert 102 < flip < 104



def test_a_non_numeric_input_is_unusable_not_an_error():
    assert gex.bs_gamma(100.0, "n/a", 1.0, 0.2) == 0.0
