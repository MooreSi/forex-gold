"""docs/todo/017 items 1 and 2, as the Reversal model sees them.

1. `re_ml_time_decay_enabled`: the batch fit gets time-decay x uniqueness
   weights instead of positional ones.
2. `re_ml_learn_from_executed`: an executed trade gets a label, the broker's
   net over the initial risk the core trade row recorded at open
   (`vantage_simulated_trades.initial_risk`, every leg counted). Off, executed
   rows stay out exactly as before (reversal-engine/250).

Off, both must leave the fit byte-identical. The executed lookup is
monkeypatched; nothing here opens the core database or reaches a broker.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.market import sample_weights as sw
from backend.src.services.reversal_engine import ml_engine as m
from backend.src.services.reversal_engine import executed_label as xl
from backend.src.services.reversal_engine.ml_engine import _training_data as td

HOUR = 3600.0


def _row(i, net, *, executed=False, vsid=None):
    width = len(m.FEATURE_NAMES)
    feats = [round(0.1 + (i % 7) * 0.05 + k * 0.001, 4) for k in range(width)]
    t0 = 1_760_000_000.0 + i * HOUR
    return {"id": i + 1, "ml_features_json": json.dumps(feats),
            "outcome": "win" if net > 0 else "loss",
            "sl_dist": 7.0, "net_pnl_dollars": net,
            "created_at": t0, "trigger_time": t0 + 60.0, "close_time": t0 + 1800.0,
            "live_exec_status": "executed" if executed else None,
            "vantage_signal_id": vsid}


def _rows():
    rows = [_row(i, 30.0 if i % 3 else -70.0) for i in range(40)]
    rows.append(_row(40, -300.0, executed=True, vsid="V-1"))
    rows.append(_row(41, 90.0, executed=True, vsid="V-2"))   # no recorded risk
    return rows


@pytest.fixture
def switches(monkeypatch):
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    state = {"decay": False, "executed": False, "lookups": []}
    monkeypatch.setattr(re_db, "get_ml_training_data", _rows)
    monkeypatch.setattr(td, "_learning_switches",
                        lambda: (state["decay"], state["executed"]))

    def _risk(vsids):
        state["lookups"].append(sorted(vsids))
        return {"V-1": (-300.0, 300.0)}
    monkeypatch.setattr(xl, "fetch_closed_risk", _risk)
    return state


class TestOffIsToday:
    def test_no_weights_and_no_executed_rows(self, switches):
        X, y, w = td._get_training_set()
        assert w is None
        assert len(y) == 40
        assert switches["lookups"] == []      # the core DB is not even asked

    def test_the_two_value_reader_is_unchanged(self, switches):
        X, y = td._get_training_data()
        X2, y2, _ = td._get_training_set()
        assert (X, y) == (X2, y2)

    def test_the_fit_is_called_exactly_as_before(self, switches, monkeypatch, tmp_path):
        calls = []
        real = m._fit_batch

        def _spy(*args):
            calls.append(len(args))
            return real(*args)
        monkeypatch.setattr(m, "_fit_batch", _spy)
        monkeypatch.setattr(m, "_data_dir", tmp_path)
        monkeypatch.setattr(m, "_model_batch", None)
        monkeypatch.setattr(m, "_train_history", [])
        m._retrain()
        assert calls == [2]


class TestTimeDecayOn:
    def test_weights_are_the_shared_definition_over_the_rows_spans(self, switches):
        switches["decay"] = True
        X, y, w = td._get_training_set()
        rows = sorted(_rows()[:40], key=lambda r: r["id"])
        spans = [(r["trigger_time"], r["close_time"]) for r in rows]
        assert w == pytest.approx(sw.training_weights(
            spans, td.DECAY_HALF_LIFE_DAYS, td.DECAY_FLOOR))
        assert len(w) == len(y)

    def test_the_fit_receives_them(self, switches, monkeypatch, tmp_path):
        switches["decay"] = True
        seen = []
        real = m._fit_batch

        def _spy(*args):
            seen.append(args[2] if len(args) > 2 else None)
            return real(*args)
        monkeypatch.setattr(m, "_fit_batch", _spy)
        monkeypatch.setattr(m, "_data_dir", tmp_path)
        monkeypatch.setattr(m, "_model_batch", None)
        monkeypatch.setattr(m, "_train_history", [])
        m._retrain()
        assert seen and seen[0] is not None and len(seen[0]) == 40

    def test_a_row_without_a_close_time_falls_back_to_creation(self):
        r = {"created_at": 100.0, "trigger_time": None, "close_time": None}
        assert td._span(r) == (100.0, 100.0)


class TestExecutedOn:
    def test_an_executed_trade_is_labelled_on_its_own_recorded_risk(self, switches):
        switches["executed"] = True
        X, y, _ = td._get_training_set()
        assert len(y) == 41
        assert y[-1] == pytest.approx(-1.0)      # -300 / 300
        assert switches["lookups"] == [["V-1", "V-2"]]

    def test_one_without_recorded_risk_stays_out(self, switches):
        switches["executed"] = True
        X, y, _ = td._get_training_set()
        assert 90.0 / 300.0 not in y and len(y) == 41

    def test_the_virtual_label_is_untouched(self, switches):
        off = td._get_training_set()[1]
        switches["executed"] = True
        on = td._get_training_set()[1]
        assert on[:40] == off


class TestTheExecutedLabel:
    def test_clamped_like_the_virtual_label(self):
        assert xl.label(-5000.0, 100.0) == -td._R_LABEL_CLAMP

    @pytest.mark.parametrize("net,risk", [(10.0, 0.0), (10.0, -5.0), (None, 10.0),
                                          (10.0, None), (float("nan"), 10.0)])
    def test_unusable_figures_give_no_label(self, net, risk):
        assert xl.label(net, risk) is None

    def test_training_label_prefers_the_virtual_label_for_a_virtual_row(self):
        r = _row(1, 35.0)
        assert td.training_label(r, {"V-1": (1.0, 1.0)}) == pytest.approx(0.5)

    def test_training_label_for_an_executed_row_without_a_map_is_none(self):
        assert td.training_label(_row(1, 35.0, executed=True, vsid="V-1")) is None
