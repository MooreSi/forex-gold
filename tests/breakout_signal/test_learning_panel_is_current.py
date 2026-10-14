"""The Breakout "Is it learning?" panel after docs/todo/017.

The chart's legend has always said "mean realised R", but the Breakout series
was the training label: rr_tp1 for a win, -1 for a loss, and rr_tp1 is 1.0 on
every row, so it plotted +1 / -1. It now plots each trade's realised R (net
dollars over its own risk), whichever label the model trains on, and says
which label that is and whether the model has earned an opinion.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import ml_engine as ml
from backend.src.services.breakout_signal import ml_labels as lb


def _row(i, outcome, net):
    # id, signal_ref, ml_prob, outcome, rr_tp1, sl_dist, lot_size,
    # net_pnl_dollars, pnl_dollars, live_exec_status
    return (i, f"BO-{i}", 0.2, outcome, 1.0, 10.0, 0.1, net, net, None)


@pytest.fixture
def panel(monkeypatch):
    from backend.src.services.breakout_signal import breakout_signal_repo as bdb
    state = {"rows": [_row(1, "win", 102.0), _row(2, "loss", -84.0)],
             "realised": False}
    monkeypatch.setattr(bdb, "fetch_ml_outcome_rows", lambda: state["rows"])
    monkeypatch.setattr(lb, "switches", lambda: (False, state["realised"]))
    monkeypatch.setattr(ml, "_label_mode", lb.LEGACY)
    monkeypatch.setattr(ml, "_gate_armed", True)
    monkeypatch.setattr(ml, "_train_history", [])
    return state


def test_the_chart_plots_realised_r_not_the_old_label(panel):
    got = ml.get_ml_metrics()
    assert got["actual_r_series"] == [pytest.approx(1.02), pytest.approx(-0.84)]


def test_a_row_without_a_realised_r_is_left_off(panel):
    panel["rows"].append((3, "BO-3", 0.2, "win", 1.0, 0.0, 0.1, 50.0, 50.0, None))
    assert ml.get_ml_metrics()["n_data"] == 2


def test_with_the_old_label_the_panel_says_so(panel):
    got = ml.get_ml_metrics()["learning"]
    assert got["label"] == "rr_tp1 / -1 / 0 (the old label)"
    assert got["armed"] is True


def test_with_realised_r_on_it_shows_the_holdout_and_whether_it_is_armed(panel, monkeypatch):
    panel["realised"] = True
    monkeypatch.setattr(ml, "_label_mode", lb.REALISED)
    monkeypatch.setattr(ml, "_gate_armed", False)
    monkeypatch.setattr(ml, "_train_history", [{"cv_auc": 0.53, "effective_n": 101.2}])
    got = ml.get_ml_metrics()["learning"]
    assert got["label"] == "realised R"
    assert got["armed"] is False
    assert got["holdout_auc"] == 0.53
    assert got["effective_n"] == 101.2
