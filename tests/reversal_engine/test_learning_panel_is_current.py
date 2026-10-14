"""The Reversal "Is it learning?" panel after docs/todo/017.

1. A real trade is plotted when its result can be put on the R scale: the core
   trade's net over the risk it recorded at open. Before 017 every executed
   trade was left off the chart, because its dollars were at the real lot.
   This is what happened with real money, so it is plotted whether or not
   the model is switched to learn from it.
2. The panel says what the model currently learns from: which label, which
   weights, and how many rows those weights are worth.

Fakes only: the core lookup is monkeypatched.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.reversal_engine import executed_label as xl
from backend.src.services.reversal_engine import ml_engine as m
from backend.src.services.reversal_engine.ml_engine import _training_data as td


def _row(i, net, *, executed=False, vsid=None):
    return {"id": i, "signal_ref": f"RE-{i}", "ml_prob": 0.1,
            "outcome": "win" if net > 0 else "loss", "rr_tp1": 1.0,
            "sl_dist": 5.0, "net_pnl_dollars": net,
            "live_exec_status": "executed" if executed else None,
            "vantage_signal_id": vsid}


@pytest.fixture
def panel(monkeypatch):
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    state = {"rows": [], "decay": False, "executed": False}
    monkeypatch.setattr(re_db, "fetch_ml_outcome_rows", lambda: state["rows"])
    monkeypatch.setattr(xl, "fetch_closed_risk", lambda ids: {"V-1": (-450.0, 300.0)})
    monkeypatch.setattr(td, "_learning_switches",
                        lambda: (state["decay"], state["executed"]))
    return state


class TestRealTradesArePlotted:
    def test_an_executed_trade_with_recorded_risk_is_on_the_chart(self, panel):
        panel["rows"] = [_row(1, 25.0), _row(2, -900.0, executed=True, vsid="V-1")]
        got = m.get_ml_metrics()
        assert got["actual_r_series"] == [pytest.approx(0.5), pytest.approx(-1.5)]
        assert got["learning"]["executed_plotted"] == 1

    def test_one_without_recorded_risk_stays_off(self, panel):
        panel["rows"] = [_row(1, 25.0), _row(3, 900.0, executed=True, vsid="V-9")]
        got = m.get_ml_metrics()
        assert got["n_data"] == 1 and got["learning"]["executed_plotted"] == 0


class TestThePanelSaysWhatTheModelLearns:
    def test_with_the_switches_off(self, panel):
        panel["rows"] = [_row(1, 25.0)]
        got = m.get_ml_metrics()["learning"]
        assert got["weights"] == "positional (newest rows weighted most)"
        assert got["learns_from_executed"] is False

    def test_with_the_switches_on(self, panel):
        panel["rows"] = [_row(1, 25.0)]
        panel.update(decay=True, executed=True)
        got = m.get_ml_metrics()["learning"]
        assert got["weights"] == "by age: half-life 14 days, floor 0.25, overlap discounted"
        assert got["learns_from_executed"] is True

    def test_an_empty_panel_still_carries_the_description(self, panel):
        assert "weights" in m.get_ml_metrics()["learning"]


class TestEffectiveSampleSize:
    def test_positional_weights_on_5431_rows_are_worth_about_118(self):
        assert td.effective_n(5431) == pytest.approx(117.6, abs=0.1)

    def test_equal_weights_are_worth_every_row(self):
        assert td.effective_n(3, [1.0, 1.0, 1.0]) == pytest.approx(3.0)

    def test_a_retrain_records_it(self, monkeypatch, tmp_path):
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        width = len(m.FEATURE_NAMES)
        rows = [{"id": i, "ml_features_json": json.dumps([0.1 * (i % 5)] * width),
                 "outcome": "win" if i % 3 else "loss", "sl_dist": 7.0,
                 "net_pnl_dollars": 30.0 if i % 3 else -70.0} for i in range(40)]
        monkeypatch.setattr(re_db, "get_ml_training_data", lambda: rows)
        monkeypatch.setattr(td, "_learning_switches", lambda: (False, False))
        monkeypatch.setattr(m, "_data_dir", tmp_path)
        monkeypatch.setattr(m, "_model_batch", None)
        monkeypatch.setattr(m, "_train_history", [])
        m._retrain()
        assert m._train_history[-1]["effective_n"] == td.effective_n(40)
