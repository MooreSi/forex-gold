"""Which rows the model and its panel read, and in what order.

1. `get_ml_metrics` ("Is it learning?") reads `fetch_ml_outcome_rows`, which
   did not select `live_exec_status`. `_realised_r` leaves executed rows out
   because their net P&L is the broker's, at the real lot and the template's
   stop, not `sl_dist` at the 0.1 virtual lot it divides by -- but it could
   only do that if the column was there. 963 executed rows on the owner's Mac
   were plotted on the wrong scale.

2. `_fit_batch` weights row i by exp(-0.017 * (n - i)): it assumes the rows
   arrive oldest first. `get_ml_training_data` has no ORDER BY, so that rested
   on SQLite's scan order. `_get_training_data` now returns them by id.

Temp database. Nothing here reaches a broker.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest import mock

import pytest

from backend.src.services.reversal_engine import ml_engine as ml
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.reversal_engine.ml_engine import _training_data as td
from tests.conftest import remove_db_file


@pytest.fixture
def repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    yield re_repo
    re_repo.close_db()
    remove_db_file(path)


def _closed(repo, ref, *, executed, net):
    sid = repo.create_signal({"signal_ref": ref, "direction": "BUY",
                              "sl_dist": 5.0, "entry_low": 1.0,
                              "entry_high": 1.0, "stop_loss": 0.5})
    repo.store_ml_prob(sid, 0.1)
    repo.close_signal(sid, 1.0, "win" if net > 0 else "loss", net_pnl_dollars=net)
    if executed:
        repo.update_live_exec(sid, status="executed")
    return sid


class TestThePanelLeavesExecutedRowsOut:
    def test_the_row_carries_its_execution_status(self, repo):
        _closed(repo, "RE-A", executed=True, net=50.0)
        assert dict(repo.fetch_ml_outcome_rows()[0])["live_exec_status"] == "executed"

    def test_only_the_virtual_row_is_plotted(self, repo):
        _closed(repo, "RE-A", executed=True, net=500.0)
        _closed(repo, "RE-B", executed=False, net=10.0)

        m = ml.get_ml_metrics()

        assert m["n_data"] == 1
        assert m["actual_r_series"] == [pytest.approx(0.2)]


def _row(id_, label_net):
    return {"id": id_, "outcome": "win", "sl_dist": 1.0,
            "net_pnl_dollars": label_net, "live_exec_status": None,
            "ml_features_json": json.dumps([0.5] * len(ml.FEATURE_NAMES))}


class TestTheTrainingRowsAreOldestFirst:
    def test_rows_returned_out_of_order_are_put_back_in_order(self):
        rows = [_row(3, 30.0), _row(1, 10.0), _row(2, 20.0)]
        with mock.patch("backend.src.services.reversal_engine.reversal_engine_repo"
                        ".get_ml_training_data", return_value=rows):
            _, y = td._get_training_data()
        assert y == [1.0, 2.0, 3.0]

    def test_rows_already_in_order_are_unchanged(self):
        rows = [_row(1, 10.0), _row(2, 20.0), _row(3, 30.0)]
        with mock.patch("backend.src.services.reversal_engine.reversal_engine_repo"
                        ".get_ml_training_data", return_value=rows):
            _, y = td._get_training_data()
        assert y == [1.0, 2.0, 3.0]
