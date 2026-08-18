"""Reward-per-unit-risk for a TP ladder — the readout under the EA Template
percentage rows.

The number exists to be designed against, so it has to be the number the EA
would actually produce. Two ways it can flatter a ladder, both covered here:

  * summing the % column instead of walking remaining lots. The EA's
    DoPartialClose takes MathMin(lots, remaining), so a level can never close
    more than is still open -- "GD Institutional - Grid" (25/25/25/100) sums
    to 4.00R that way and really banks 1.50R; and
  * ignoring close_full_on_last, which makes the deepest configured level bank
    whatever is still open regardless of its own %.

total_r is the all-levels-hit figure and deliberately not probability-weighted:
it says what the geometry pays when it works.
"""
import pytest

from forex_trader.core.core_ea_templates import ladder_rr


def _levels(pips_pcts):
    return [(i, p, q) for i, (p, q) in enumerate(pips_pcts, start=1)]


# ── the basic arithmetic ─────────────────────────────────────────────────────

def test_r_at_each_level_is_reward_over_risk():
    r = ladder_rr(40, _levels([(40, 50), (80, 30), (130, 20)]))
    assert [round(row[1], 4) for row in r["rows"]] == [1.0, 2.0, 3.25]


def test_weighted_contribution_is_r_times_the_slice_closed_there():
    r = ladder_rr(40, _levels([(40, 50), (80, 30), (130, 20)]))
    assert [round(row[3], 4) for row in r["rows"]] == [0.5, 0.6, 0.65]
    assert r["total_r"] == pytest.approx(1.75)


def test_halving_the_stop_doubles_every_r():
    wide = ladder_rr(40, _levels([(40, 50), (80, 30), (130, 20)]))
    tight = ladder_rr(20, _levels([(40, 50), (80, 30), (130, 20)]))
    assert tight["total_r"] == pytest.approx(wide["total_r"] * 2)


def test_a_target_inside_the_stop_is_worth_less_than_1r():
    """Asian Reversal - ATR banks 45% of the position at 0.50R and 0.83R,
    which is why its ladder totals 1.15R off five targets."""
    r = ladder_rr(120, _levels([(60, 20), (100, 25), (150, 25), (220, 15), (300, 10)]),
                  close_full_on_last=False)
    assert r["rows"][0][1] == pytest.approx(0.5)
    assert r["total_r"] == pytest.approx(1.1458, abs=1e-3)
    assert r["remaining"] == pytest.approx(5.0)


# ── remaining-lots behaviour ─────────────────────────────────────────────────

def test_percentages_over_100_cannot_close_more_than_is_left():
    """The regression this guards: summing gives 4.00R, the EA banks 1.50R."""
    lv = _levels([(20, 25), (50, 25), (90, 25), (200, 100)])
    r = ladder_rr(60, lv, close_full_on_last=True)
    assert r["pct_sum"] == 175.0
    assert r["total_r"] == pytest.approx(1.5)
    assert r["rows"][-1][2] == pytest.approx(25.0), "last level closed more than remained"
    assert r["remaining"] == 0.0


def test_a_middle_level_is_capped_at_what_earlier_levels_left():
    """The cap has to bite on levels that are NOT the last one. With
    close_full_on_last on, the deepest level takes the remaining-lots branch
    regardless, so an over-close there proves nothing about the cap -- this
    puts the greed in the middle of the ladder, where only min(pct, remaining)
    can stop it."""
    lv = _levels([(40, 80), (80, 80), (130, 10)])
    r = ladder_rr(40, lv, close_full_on_last=False)
    closed = {n: c for n, _rr, c, _k in r["rows"]}
    assert closed[1] == pytest.approx(80.0)
    assert closed[2] == pytest.approx(20.0), "TP2 closed more than was left"
    assert closed.get(3, 0.0) == pytest.approx(0.0)
    assert r["total_r"] == pytest.approx(1.0 * 0.80 + 2.0 * 0.20)
    assert r["remaining"] == 0.0


def test_total_never_exceeds_the_deepest_levels_r():
    """A sanity bound: closing 100% of the position at the best price on the
    ladder is the most any ladder can pay. Anything above that means slices
    were double-counted."""
    for lv, cf in (
        (_levels([(40, 80), (80, 80), (130, 80)]), False),
        (_levels([(40, 60), (80, 60)]), True),
        (_levels([(20, 25), (50, 25), (90, 25), (200, 100)]), True),
    ):
        r = ladder_rr(40, lv, close_full_on_last=cf)
        best = max(pips for _n, pips, _p in lv) / 40
        assert r["total_r"] <= best + 1e-9, f"{r['total_r']} > best single level {best}"


def test_slices_closed_never_add_to_more_than_the_whole_position():
    lv = _levels([(40, 80), (80, 80), (130, 80)])
    for cf in (True, False):
        r = ladder_rr(40, lv, close_full_on_last=cf)
        assert sum(c for _n, _rr, c, _k in r["rows"]) <= 100.0 + 1e-9


def test_close_full_on_last_banks_the_spare_at_the_deepest_level():
    lv = _levels([(40, 20), (80, 25), (130, 25)])
    r = ladder_rr(40, lv, close_full_on_last=True)
    assert r["rows"][-1][2] == pytest.approx(55.0)   # its 25 + the spare 30
    assert r["remaining"] == 0.0


def test_close_full_off_leaves_a_genuine_runner():
    lv = _levels([(40, 20), (80, 25), (130, 25)])
    r = ladder_rr(40, lv, close_full_on_last=False)
    assert r["rows"][-1][2] == pytest.approx(25.0)
    assert r["remaining"] == pytest.approx(30.0)
    assert r["total_r"] < ladder_rr(40, lv, close_full_on_last=True)["total_r"]


def test_a_level_can_never_close_a_negative_or_over_full_slice():
    for lv in (_levels([(40, 300)]), _levels([(40, 50), (80, 500)])):
        r = ladder_rr(40, lv)
        assert all(0 <= row[2] <= 100 for row in r["rows"])
        assert 0 <= r["remaining"] <= 100


# ── levels that are not really levels ────────────────────────────────────────

def test_a_zero_pip_level_contributes_nothing_and_keeps_its_lots():
    """Sig Gen Grid runs TP5 at 0 pips and lets the ladder carry on to TP6."""
    r = ladder_rr(50, _levels([(100, 10), (0, 40), (200, 20)]))
    assert [row[0] for row in r["rows"]] == [1, 3]
    assert r["total_r"] == pytest.approx(100 / 50 * 0.10 + 200 / 50 * 0.90)


def test_no_levels_gives_a_zero_total_and_a_full_runner():
    r = ladder_rr(40, [])
    assert r["total_r"] == 0.0 and r["remaining"] == 100.0 and r["rows"] == []


@pytest.mark.parametrize("sl", [0, None, -10, "", "abc"])
def test_without_a_usable_stop_there_is_no_r_to_report(sl):
    """R is a ratio against the stop. Inventing one when the stop is unset
    would put a confident number under a ladder that has no risk defined."""
    r = ladder_rr(sl, _levels([(40, 50), (80, 50)]))
    assert r["total_r"] == 0.0 and r["rows"] == []


def test_none_percentages_are_treated_as_zero_not_crashed_on():
    """An empty ui.number reports None on every keystroke that clears it."""
    r = ladder_rr(40, [(1, 40, None), (2, 80, 50)])
    assert r["total_r"] > 0


# ── the live templates ───────────────────────────────────────────────────────

@pytest.mark.parametrize("sl,ladder,close_full,expected", [
    (35,  [(35, 60), (70, 40)],                                   True, 1.40),   # Auto Limit Scalp
    (40,  [(40, 50), (80, 30), (130, 20)],                        True, 1.75),   # Auto Limit Balanced
    (50,  [(50, 30), (100, 25), (180, 25), (300, 20)],            True, 2.90),   # Auto Limit Trend
    (60,  [(60, 40), (120, 30), (200, 30)],                       True, 2.00),   # Auto Market Runner
    (120, [(60, 20), (100, 25), (150, 25), (220, 15), (300, 10)], False, 1.146),  # Asian Reversal - ATR
])
def test_matches_the_shipped_templates(sl, ladder, close_full, expected):
    r = ladder_rr(sl, _levels(ladder), close_full_on_last=close_full)
    assert r["total_r"] == pytest.approx(expected, abs=0.01)
