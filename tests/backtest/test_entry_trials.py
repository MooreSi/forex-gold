"""Synthetic UTC quotes and independent opportunities; no network or orders."""
import pytest
from backend.src.services.backtest import entry_trials as e


def quote(ts, bid, ask=None):
    return {"ts": ts, "available_at": ts, "bid": bid, "ask": bid + .2 if ask is None else ask}


def opportunity(**kw):
    return {"id": "one", "decision_ts": 0, "context_available_at": 0,
            "family": "structural_pullback", "direction": "BUY", "stop": 98,
            "atr": 2, "cost_pts": .1, "context": {"fresh_break": True,
                "touch_count": 0, "trend_aligned": True, "room_r": 3}, **kw}


def test_first_pullback_requires_the_entire_context():
    c = opportunity()["context"]
    assert e.qualifies("structural_pullback", c)
    assert not e.qualifies("structural_pullback", {**c, "touch_count": 1})
    assert not e.qualifies("structural_pullback", {**c, "room_r": 1})


def test_breakout_acceptance_is_not_a_sweep_or_another_touch():
    c = {"fresh_break": True, "touch_count": 0, "acceptance_closes": 2, "compression_atr": 2}
    assert e.qualifies("breakout_retest", c)
    assert not e.qualifies("breakout_retest", {**c, "acceptance_closes": 0})


def test_reversal_needs_observed_flow_and_failed_continuation():
    c = {"failed_continuation": True, "spread_recovered": True, "flow_against_break": True,
         "flow_source": "exchange", "flow_age_s": .5}
    assert e.qualifies("failed_continuation", c)
    assert not e.qualifies("failed_continuation", {**c, "flow_source": "tick_count"})
    assert not e.qualifies("failed_continuation", {**c, "flow_age_s": 60})


def test_executable_quotes_pay_spread_once_and_incremental_costs_once():
    result = e.simulate(opportunity(), [quote(0, 100), quote(3600, 100)])
    assert result["filled"] is True
    assert result["r"] == pytest.approx(-.3 / 2.2)


def test_waiting_order_can_expire_without_a_fill():
    result = e.simulate(opportunity(), [quote(0, 100), quote(300, 101), quote(3600, 102)], wait=True)
    assert result["filled"] is False and result["r"] == 0
    assert result["exit_ts"] == 300


def test_limit_cannot_enter_after_structural_invalidation():
    result = e.simulate(opportunity(), [quote(0, 100), quote(30, 97.5), quote(3600, 105)], wait=True)
    assert result["filled"] is False
    assert result["reason"] == "invalidated before fill"


def test_quote_gap_fills_a_stop_at_the_first_executable_price():
    result = e.simulate(opportunity(), [quote(0, 100), quote(10, 95), quote(3600, 105)])
    assert result["r"] == pytest.approx((-5.2 - .1) / 2.2)


def test_incomplete_paths_are_censored_and_never_called_zero_return():
    result = e.simulate(opportunity(), [quote(0, 100), quote(100, 101)])
    assert result["censored"] is True
    assert result["r"] is None


def test_context_from_the_future_is_rejected():
    with pytest.raises(ValueError, match="available"):
        e.run([opportunity(context_available_at=1)], [quote(0, 100), quote(3600, 102)])


def test_unknown_costs_are_not_a_free_trade():
    with pytest.raises(ValueError, match="cost"):
        e.simulate(opportunity(cost_pts=None), [quote(0, 100)])


def test_losing_or_thin_evidence_can_never_qualify():
    rows = [{"decision_ts": e.HOLDOUT_START + i * 86400, "r": -1, "baseline_r": 0, "risk": 2,
             "filled": True, "forward": True, "censored": False} for i in range(300)]
    assert e.judge(rows)["review_eligible"] is False
    assert e.judge([{**r, "r": 1} for r in rows[:5]])["review_eligible"] is False


def test_strong_forward_evidence_can_qualify_for_review_but_never_deploy():
    rows = [{"decision_ts": e.HOLDOUT_START + i * 86400, "r": .8 + .1 * (i % 2), "baseline_r": -.2,
             "risk": 2, "filled": True, "forward": True, "censored": False} for i in range(300)]
    result = e.judge(rows)
    assert result["review_eligible"] is True
    assert result["deployable"] is False


def test_nonfinite_context_time_cannot_bypass_arrival_check():
    with pytest.raises(ValueError, match="finite"):
        e.run([opportunity(context_available_at=float("nan"))], [quote(0, 100)])


def test_quote_numeric_strings_have_the_same_order_and_result():
    path = [quote(0, 100), quote(3600, 100)]
    strings = [{k: str(v) for k, v in q.items()} for q in path]
    assert e.simulate(opportunity(), strings) == e.simulate(opportunity(), path)


def test_chronological_metrics_do_not_depend_on_input_row_order():
    rows = [{"decision_ts": e.HOLDOUT_START + i * 86400, "r": r, "baseline_r": 0,
             "risk": 2, "filled": True, "forward": True, "censored": False}
            for i, r in enumerate([3, -2, -2, 3])]
    assert e.judge(list(reversed(rows))) == e.judge(rows)
