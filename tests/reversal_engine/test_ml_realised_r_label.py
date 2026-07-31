"""ml_engine's v5 realised-R label and the repaired ref_level_win_rate.

Pure-function tests over _realised_r plus the ref pattern counters. No model
is trained, no DB is written, and nothing here places or modifies a trade.

Context: up to v4 the label was `rr_tp1 if win else -1.0`, which priced every
loss at exactly -1.0R regardless of how far past the stop it actually filled,
and priced every win at its planned target regardless of how much of the
position was still on at that point. Summed over the 576 real closed signals
that label read +51.1 while the same trades lost $2,691.
"""
import pytest

from forex_trader.reversal_engine import ml_engine as m


# ── realised R label ─────────────────────────────────────────────────────

def _row(sl_dist, net):
    return {"sl_dist": sl_dist, "net_pnl_dollars": net}


def test_full_stop_out_is_minus_one_r():
    # 7.0 pts risked at $10/pt = $70; losing exactly that is -1.00R.
    assert m._realised_r(_row(7.0, -70.0)) == pytest.approx(-1.0)


def test_loss_beyond_the_stop_exceeds_minus_one_r():
    """The defect the old label hid: real stops fill past sl_dist. Signal 693
    risked $70 and lost $90.30, which is -1.29R, not the -1.0 it was taught."""
    assert m._realised_r(_row(7.0, -90.30)) == pytest.approx(-1.29)


def test_scaled_out_win_is_worth_less_than_its_planned_target():
    """Signal 688 hit TP1 with 50% still open and banked $9.30 against $70
    risked. The old label would have called this +0.43R (its planned rr_tp1);
    it is really +0.13R."""
    assert m._realised_r(_row(7.0, 9.30)) == pytest.approx(0.1329, abs=1e-4)


def test_breakeven_close_is_near_zero():
    assert m._realised_r(_row(7.0, 0.0)) == 0.0


def test_costs_can_push_a_nominal_scratch_negative():
    assert m._realised_r(_row(7.0, -0.95)) < 0


def test_missing_or_zero_risk_yields_no_label():
    """A row that cannot express risk must be dropped from training rather
    than silently labelled 0.0, which would teach the model that a signal it
    knows nothing about was a scratch."""
    assert m._realised_r(_row(None, -70.0)) is None
    assert m._realised_r(_row(0.0, -70.0)) is None
    assert m._realised_r(_row(-3.0, -70.0)) is None
    assert m._realised_r(_row(7.0, None)) is None
    assert m._realised_r({}) is None


def test_non_numeric_fields_do_not_raise():
    assert m._realised_r(_row("abc", -70.0)) is None
    assert m._realised_r(_row(7.0, "abc")) is None


def test_absurd_values_are_clamped_not_propagated():
    """Guards a corrupt row from dominating the regressor, while sitting well
    outside the real observed range (-5.75R to +5.01R) so genuine tails pass
    through untouched."""
    assert m._realised_r(_row(0.01, 1e9)) == m._R_LABEL_CLAMP
    assert m._realised_r(_row(0.01, -1e9)) == -m._R_LABEL_CLAMP
    assert m._realised_r(_row(7.0, -402.30)) == pytest.approx(-5.747, abs=1e-3)


def test_dollars_per_point_matches_the_engines_own_pnl_model():
    """reversal_engine_manage._net_pnl computes gross as
    pnl_pts * _VIRTUAL_LOT * 100. If those drift apart every label silently
    rescales, so pin the relationship."""
    from forex_trader.reversal_engine import reversal_engine_manage as mg
    assert m._DOLLARS_PER_POINT == mg._VIRTUAL_LOT * 100


# ── ref_level_win_rate ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_ref_stats():
    saved = dict(m._ref_level_stats)
    m._ref_level_stats.clear()
    yield
    m._ref_level_stats.clear()
    m._ref_level_stats.update(saved)


def test_unseen_level_type_uses_the_neutral_prior():
    assert m._ref_win_rate_for_type("round_5") == 0.65


def test_wins_are_credited_so_the_rate_is_no_longer_stuck_at_zero():
    """The v4 bug: record_ref_signal was only ever called with was_win=None,
    so `wins` stayed 0 while `trades` climbed, and this returned a hard 0.0
    for exactly the level types the reference channel trades most."""
    for _ in range(4):
        m.record_ref_signal("round_5")           # match observed
    m.record_ref_signal("round_5", was_win=True)  # and it won
    m.record_ref_signal("round_5", was_win=True)
    assert m._ref_win_rate_for_type("round_5") == pytest.approx(0.5)


def test_losses_do_not_inflate_the_win_count():
    for _ in range(4):
        m.record_ref_signal("swing_low")
    m.record_ref_signal("swing_low", was_win=False)
    assert m._ref_win_rate_for_type("swing_low") == 0.0


def test_prior_holds_until_enough_matches_accumulate():
    m.record_ref_signal("asia_high")
    m.record_ref_signal("asia_high", was_win=True)
    # Only 1 match: too thin to trust, so the prior stands rather than 100%.
    assert m._ref_win_rate_for_type("asia_high") == 0.65


def test_touches_are_counted_separately_from_matches():
    """touches is the denominator for ref_match_rate_for_type: how often this
    level type predicts a REF signal at all, as opposed to how those matches
    then resolved."""
    for _ in range(10):
        m.record_level_touch("congestion")
    m.record_ref_signal("congestion")
    m.record_ref_signal("congestion")
    assert m._ref_level_stats["congestion"]["touches"] == 10
    assert m._ref_level_stats["congestion"]["trades"] == 2
    assert m.ref_match_rate_for_type("congestion") == pytest.approx(0.2)


def test_match_rate_withheld_until_enough_touches():
    m.record_level_touch("round_10")
    assert m.ref_match_rate_for_type("round_10") is None
