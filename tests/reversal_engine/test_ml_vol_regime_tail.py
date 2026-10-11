"""The Reversal ML model and the `vol_regime` tail. docs/todo/016 phase 2.

The load-bearing property: with `re_vol_regime_feature_enabled` OFF, the
model sees exactly what it saw before the feature existed. Same training
width, same online update, same vector at predict. `predict` itself is unchanged; the call sites wrap
the vector with `with_vol_tail`, because `test_ml_fill_time_rescore.py`
patches `predict` with a one-argument double and is not edited. On, the tail is the
stored `atr_ratio` for training rows and the live ratio for a scored
signal, and the switch can move in either direction without the gate
failing open while the installed model is still the other width.

Fake models and a temp database. Nothing here reaches a broker.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

import pytest

from backend.src.services.reversal_engine import ml_engine as ml
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.reversal_engine import vol_regime as vr
from backend.src.services.reversal_engine.ml_engine import _training_data as td
from tests.conftest import remove_db_file

BASE = len(ml.FEATURE_NAMES)
_ROOT = Path(__file__).resolve().parents[2]


class _Recorder:
    """A fitted-looking model that records the vector it is asked to score."""

    def __init__(self, width):
        self.n_features_in_ = width
        self.seen = []

    def predict(self, fa):
        self.seen.append(list(fa[0]))
        if fa.shape[1] != self.n_features_in_:
            raise ValueError(f"width {fa.shape[1]} != {self.n_features_in_}")
        return [0.25]


@pytest.fixture
def models(monkeypatch):
    def install(width):
        batch = _Recorder(width)
        monkeypatch.setattr(ml, "_model_batch", batch)
        monkeypatch.setattr(ml, "_model_online", None)
        return batch
    return install


def _switch(monkeypatch, on, ref=5.0):
    monkeypatch.setattr(vr, "feature_enabled", lambda now=None: on)
    monkeypatch.setattr(vr, "reference_atr_now", lambda now=None: ref)


_FEATS = [0.5] * BASE
_SIG = {"atr": 10.0}   # ratio 2.0 against a reference of 5.0


class TestPredictWithTheSwitchOff:
    def test_the_model_sees_the_base_vector(self, monkeypatch, models):
        _switch(monkeypatch, False)
        batch = models(BASE)
        assert ml.predict(ml.with_vol_tail(list(_FEATS), _SIG)) == 0.25
        assert batch.seen == [_FEATS]

    def test_it_is_identical_to_scoring_the_bare_vector(self, monkeypatch, models):
        _switch(monkeypatch, False)
        batch = models(BASE)
        ml.predict(ml.with_vol_tail(list(_FEATS), _SIG))
        ml.predict(list(_FEATS))
        assert batch.seen[0] == batch.seen[1]
        assert ml.with_vol_tail(list(_FEATS), _SIG) == _FEATS

    def test_a_model_still_carrying_the_tail_gets_the_neutral(self, monkeypatch, models):
        """Switched off after a retrain with the tail: scored, not raised.
        A raise returns None and the gate fails open."""
        _switch(monkeypatch, False)
        batch = models(BASE + 1)
        assert ml.predict(ml.with_vol_tail(list(_FEATS), _SIG)) == 0.25
        assert batch.seen[0] == _FEATS + [vr.NEUTRAL]


class TestPredictWithTheSwitchOn:
    def test_a_model_not_yet_retrained_scores_the_base_vector(self, monkeypatch, models):
        _switch(monkeypatch, True)
        batch = models(BASE)
        assert ml.predict(ml.with_vol_tail(list(_FEATS), _SIG)) == 0.25
        assert batch.seen[0] == _FEATS

    def test_a_retrained_model_gets_the_live_ratio(self, monkeypatch, models):
        _switch(monkeypatch, True)
        batch = models(BASE + 1)
        assert ml.predict(ml.with_vol_tail(list(_FEATS), _SIG)) == 0.25
        assert batch.seen[0][:BASE] == _FEATS
        assert batch.seen[0][BASE] == pytest.approx(math.log(2.0), abs=1e-6)


# ── Training ─────────────────────────────────────────────────────────────────

@pytest.fixture
def repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    yield re_repo
    re_repo.close_db()
    remove_db_file(path)


def _closed(repo, ref, ratio):
    sid = repo.create_signal({"signal_ref": ref, "direction": "BUY",
                              "sl_dist": 5.0, "entry_low": 1.0,
                              "entry_high": 1.0, "stop_loss": 0.5})
    repo.store_ml_features(sid, [0.5] * BASE)
    repo.close_signal(sid, 1.0, "win", net_pnl_dollars=10.0)
    repo.get_db().run("UPDATE re_signals SET atr_ratio=? WHERE id=?", ratio, sid)
    return sid


class TestTrainingRows:
    def test_off_the_rows_keep_their_width(self, monkeypatch, repo):
        _switch(monkeypatch, False)
        _closed(repo, "RE-1", 2.0)
        X, _ = td._get_training_data()
        assert [len(x) for x in X] == [BASE]

    def test_on_each_row_carries_its_stored_ratio(self, monkeypatch, repo):
        _switch(monkeypatch, True)
        monkeypatch.setattr(vr, "backfill", lambda limit=2000: 0)
        _closed(repo, "RE-1", 2.0)
        _closed(repo, "RE-2", 0.5)
        X, _ = td._get_training_data()
        assert [len(x) for x in X] == [BASE + 1, BASE + 1]
        assert X[0][BASE] == pytest.approx(math.log(2.0), abs=1e-6)
        assert X[1][BASE] == pytest.approx(math.log(0.5), abs=1e-6)

    def test_on_the_history_is_measured_before_it_is_read(self, monkeypatch, repo):
        """The switch reuses the stored history: a row the sweep has not
        reached yet is measured on the way into training, not read as
        neutral."""
        _switch(monkeypatch, True)
        sid = _closed(repo, "RE-1", None)
        repo.get_db().run("UPDATE re_signals SET atr_ratio=NULL WHERE id=?", sid)
        called = []
        monkeypatch.setattr(vr, "backfill", lambda limit=2000: called.append(limit) or 0)
        td._get_training_data()
        assert called


class TestTheOnlineUpdate:
    def test_off_it_learns_the_base_width(self, monkeypatch, repo):
        _switch(monkeypatch, False)
        monkeypatch.setattr(ml, "_model_online", None)
        monkeypatch.setattr(ml, "_save_all", lambda: None)
        sid = _closed(repo, "RE-1", 2.0)
        ml.record_outcome(sid, "win")
        assert ml._model_online.n_features_in_ == BASE

    def test_on_it_learns_the_tail_too(self, monkeypatch, repo):
        _switch(monkeypatch, True)
        monkeypatch.setattr(ml, "_model_online", None)
        monkeypatch.setattr(ml, "_save_all", lambda: None)
        sid = _closed(repo, "RE-1", 2.0)
        ml.record_outcome(sid, "win")
        assert ml._model_online.n_features_in_ == BASE + 1


class TestBothScoringSitesPassTheSignal:
    """The live ratio comes from the signal being scored. A call site that
    drops it scores with the switch on as if it were off, silently."""

    @pytest.mark.parametrize("path,call", [
        ("backend/src/services/reversal_engine/reversal_engine_service.py",
         "re_ml.predict(re_ml.with_vol_tail(feats, feat_input))"),
        ("backend/src/services/reversal_engine/reversal_engine_live_execute.py",
         "re_ml.predict(re_ml.with_vol_tail(fresh_feats, fresh_sig))"),
    ])
    def test_call_site(self, path, call):
        assert call in (_ROOT / path).read_text(encoding="utf-8")
