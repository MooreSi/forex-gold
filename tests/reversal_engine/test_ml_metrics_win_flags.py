"""`get_ml_metrics` hands the panel raw per-signal outcomes, not a cumulative
mean it has already collapsed.

The "Is it learning?" chart used to be fed `win_rate_series`, a cumulative
win rate. At 4,880 labelled signals that series is a constant, and a
cumulative mean cannot be un-averaged back into the per-signal facts a
rolling window needs -- `wins_i * (i+1) / 100` recovers them only up to the
0.1 rounding already applied, and the error compounds along the series.

So the raw flags travel instead, and the panel decides the window.
See `frontend/components/learning_chart.py`.
"""
from __future__ import annotations

from unittest import mock

from backend.src.services.reversal_engine import ml_engine


def _rows(outcomes):
    return [{"id": i, "signal_ref": f"RE-{i}", "outcome": o, "ml_prob": 0.1,
             "pnl_dollars": 10.0 if o == "win" else -10.0}
            for i, o in enumerate(outcomes)]


def _metrics(outcomes, realised):
    with mock.patch("backend.src.services.reversal_engine."
                    "reversal_engine_repo.fetch_ml_outcome_rows",
                    return_value=_rows(outcomes)), \
         mock.patch.object(ml_engine, "_realised_r",
                           side_effect=lambda row: realised[int(row["id"])]):
        return ml_engine.get_ml_metrics()


class TestTheRawOutcomesReachThePanel:
    def test_a_win_flag_is_one_and_a_loss_is_zero(self):
        m = _metrics(["win", "loss", "win"], [1.0, -1.0, 2.0])
        assert m["win_flag_series"] == [1, 0, 1]

    def test_there_is_one_flag_per_signal(self):
        m = _metrics(["win", "loss", "win"], [1.0, -1.0, 2.0])
        assert len(m["win_flag_series"]) == len(m["signal_ids"])

    def test_the_flags_stay_in_step_with_the_realised_r_series(self):
        """The chart rolls both over the same window, so a row dropped from
        one and kept in the other would silently misalign the two lines."""
        m = _metrics(["win", "loss", "win"], [1.0, -1.0, 2.0])
        assert len(m["win_flag_series"]) == len(m["actual_r_series"])

    def test_realised_r_keeps_enough_precision_to_average(self):
        """Rounded to 1dp, a rolling mean of fifty small R values is mostly
        rounding error. The table still formats it to 1dp for display."""
        m = _metrics(["win"], [0.123456])
        assert m["actual_r_series"][0] == 0.123

    def test_no_rows_gives_an_empty_flag_series_rather_than_a_missing_key(self):
        with mock.patch("backend.src.services.reversal_engine."
                        "reversal_engine_repo.fetch_ml_outcome_rows",
                        return_value=[]):
            assert ml_engine.get_ml_metrics()["win_flag_series"] == []
