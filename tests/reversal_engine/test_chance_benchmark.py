"""Does the Reversal Engine beat chance? docs/todo/reversal-engine/220.

A driftless random walk between a stop SL away and a target TP away reaches
the target first with probability SL / (SL + TP). On 2026-09-22 that predicted
75.4% over 1,902 signals and the engine won 75.2%: the win rate was geometry.
This module is what makes that visible, and these pin its arithmetic.
"""
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import chance_benchmark as cb
from backend.src.services.reversal_engine import reversal_engine_repo as repo
from tests.conftest import remove_db_file

_T0 = 1_790_000_000.0          # 2026-09-21 14:13 UTC


def _row(outcome="win", sl=6.0, tp1=2102.0, entry=2100.0, **over):
    row = {"outcome": outcome, "sl_dist": sl, "tp1": tp1,
           "trigger_price": entry, "price_at_signal": entry,
           "direction": "BUY", "level_type": "round_5", "session": "london",
           "htf_bias": "bullish", "htf_bias_at_fill": "bullish",
           "trigger_time": _T0, "created_at": _T0,
           "live_exec_status": None, "net_pnl_dollars": 10.0, "ml_prob": None}
    row.update(over)
    return row


def _group(report, window, key, name):
    for g in report[window]["groups"][key]:
        if g["name"] == name:
            return g
    raise AssertionError(f"no group {key}={name}")


class TestTheChanceOfWinning:
    def test_it_is_the_stop_over_stop_plus_target(self):
        # 6-point stop, 2-point target: 6 / 8
        assert cb.chance_of_target(_row()) == pytest.approx(0.75)

    def test_the_target_is_measured_from_the_fill(self):
        assert cb.chance_of_target(
            _row(trigger_price=2099.0, price_at_signal=2090.0)) == pytest.approx(6 / 9)

    def test_an_unfilled_row_falls_back_to_the_signal_price(self):
        assert cb.chance_of_target(
            _row(trigger_price=None, price_at_signal=2098.0)) == pytest.approx(6 / 10)

    @pytest.mark.parametrize("bad", [
        {"sl_dist": 0.0}, {"sl_dist": None}, {"tp1": None},
        {"tp1": 2100.0},                       # target AT the entry
        {"trigger_price": None, "price_at_signal": None},
    ])
    def test_a_row_without_both_distances_has_no_chance_at_all(self, bad):
        """Not p = 1 and not p = 0: excluded. A zero-distance target would
        otherwise count as a certain win the engine 'failed' to beat."""
        assert cb.chance_of_target(_row(**bad)) is None


class TestTheScore:
    def test_winning_exactly_the_expected_rate_is_zero_excess(self):
        rows = [_row("win")] * 75 + [_row("loss")] * 25      # p = 0.75 each
        g = cb.score(rows)
        assert g["n"] == 100
        assert g["expected_win_pct"] == pytest.approx(75.0)
        assert g["actual_win_pct"] == pytest.approx(75.0)
        assert g["excess_pct"] == pytest.approx(0.0)
        assert g["z"] == pytest.approx(0.0)
        assert g["verdict"] == "no evidence"

    def test_a_group_that_wins_far_more_than_chance_beats_it(self):
        rows = [_row("win", sl=2.0, tp1=2102.0)] * 200      # p = 0.5, all win
        g = cb.score(rows)
        assert g["z"] > 3
        assert g["verdict"] == "beats chance"

    def test_a_group_that_wins_far_less_is_worse_than_chance(self):
        rows = [_row("loss", sl=2.0, tp1=2102.0)] * 200
        assert cb.score(rows)["verdict"] == "worse than chance"

    def test_under_a_hundred_rows_is_too_few_whatever_the_result(self):
        rows = [_row("win", sl=2.0, tp1=2102.0)] * 99
        assert cb.score(rows)["verdict"] == "too few"

    def test_the_bar_is_z_three_not_two(self):
        """~40 groups are tested at once; z >= 2 clears by luck every load."""
        # p = 0.5, n = 400: sd = 10, so 225 wins is z = 2.5
        rows = ([_row("win", sl=2.0, tp1=2102.0)] * 225
                + [_row("loss", sl=2.0, tp1=2102.0)] * 175)
        g = cb.score(rows)
        assert g["z"] == pytest.approx(2.5)
        assert g["verdict"] == "no evidence"

    def test_mean_net_is_reported_beside_the_win_rate(self):
        """Beating chance on wins is necessary, not sufficient."""
        rows = [_row("win", net_pnl_dollars=10.0), _row("loss", net_pnl_dollars=-30.0)]
        assert cb.score(rows)["mean_net"] == pytest.approx(-10.0)


class TestTheGroups:
    def test_bias_alignment(self):
        rows = [_row(direction="BUY", htf_bias_at_fill="bullish"),
                _row(direction="SELL", htf_bias_at_fill="bullish"),
                _row(direction="SELL", htf_bias_at_fill="neutral"),
                _row(direction="BUY", htf_bias_at_fill=None, htf_bias=None)]
        rep = cb.benchmark(rows, now=_T0 + 60)
        assert _group(rep, "all", "bias", "with")["n"] == 1
        assert _group(rep, "all", "bias", "against")["n"] == 1
        assert _group(rep, "all", "bias", "neutral")["n"] == 2

    def test_bias_at_fill_is_preferred_over_bias_at_creation(self):
        rep = cb.benchmark([_row(direction="BUY", htf_bias="bearish",
                                 htf_bias_at_fill="bullish")], now=_T0 + 60)
        assert _group(rep, "all", "bias", "with")["n"] == 1

    def test_executed_trades_are_kept_out_of_the_verdict(self):
        """An executed trade is closed by the EA template, whose target and
        stop are not the engine's `tp1`/`sl_dist`. Scored against the
        engine's geometry its chance rate is wrong -- on 2026-09-23 that made
        874 executed trades read as z = -11 and dragged the overall verdict
        with them. So they are reported apart, flagged, and not grouped."""
        rows = [_row(live_exec_status="executed"), _row(live_exec_status="ml_skipped"),
                _row(live_exec_status=None)]
        rep = cb.benchmark(rows, now=_T0 + 60)
        assert rep["all"]["overall"]["n"] == 2
        assert sum(g["n"] for g in rep["all"]["groups"]["session"]) == 2
        assert rep["all"]["executed"]["n"] == 1
        assert rep["all"]["executed"]["geometry"] == "engine"
        assert "execution" not in rep["all"]["groups"]

    def test_the_hour_is_the_fill_hour_in_utc(self):
        rep = cb.benchmark([_row()], now=_T0 + 60)       # 14:13 UTC
        assert _group(rep, "all", "hour", "14")["n"] == 1

    def test_the_recent_window_holds_only_the_last_fourteen_days(self):
        old = _row(trigger_time=_T0 - 20 * 86400, created_at=_T0 - 20 * 86400)
        rep = cb.benchmark([old, _row()], now=_T0 + 60)
        assert rep["all"]["overall"]["n"] == 2
        assert rep["recent"]["overall"]["n"] == 1
        assert rep["recent"]["days"] == 14

    def test_breakeven_and_unscorable_rows_are_left_out(self):
        rep = cb.benchmark([_row(outcome="be"), _row(sl_dist=0.0), _row()],
                           now=_T0 + 60)
        assert rep["all"]["overall"]["n"] == 1


class TestTheModelsOwnRecord:
    def test_perfectly_ranked_scores_are_one(self):
        rows = [_row(ml_prob=0.9, net_pnl_dollars=5.0),
                _row(ml_prob=-0.2, net_pnl_dollars=-5.0)]
        assert cb.benchmark(rows, now=_T0 + 60)["all"]["ml_auc"]["auc"] == pytest.approx(1.0)

    def test_reversed_scores_are_zero(self):
        rows = [_row(ml_prob=-0.2, net_pnl_dollars=5.0),
                _row(ml_prob=0.9, net_pnl_dollars=-5.0)]
        assert cb.benchmark(rows, now=_T0 + 60)["all"]["ml_auc"]["auc"] == pytest.approx(0.0)

    def test_a_row_with_no_score_is_left_out_not_scored_zero(self):
        rows = [_row(ml_prob=0.9, net_pnl_dollars=5.0),
                _row(ml_prob=-0.2, net_pnl_dollars=-5.0),
                _row(ml_prob=None, net_pnl_dollars=50.0)]
        auc = cb.benchmark(rows, now=_T0 + 60)["all"]["ml_auc"]
        assert auc["n"] == 2
        assert auc["auc"] == pytest.approx(1.0)

    def test_no_scores_at_all_is_no_answer(self):
        assert cb.benchmark([_row()], now=_T0 + 60)["all"]["ml_auc"]["auc"] is None


@pytest.fixture
def re_db_fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


class TestTheRepoRead:
    def test_it_returns_closed_rows_with_the_columns_the_benchmark_needs(self, re_db_fresh):
        rows = cb_rows_after_insert(re_db_fresh)
        assert len(rows) == 1
        need = {"outcome", "sl_dist", "tp1", "trigger_price", "price_at_signal",
                "direction", "level_type", "session", "htf_bias", "htf_bias_at_fill",
                "trigger_time", "created_at", "live_exec_status",
                "net_pnl_dollars", "ml_prob"}
        assert need <= set(rows[0])


def cb_rows_after_insert(repo):
    from backend.src.services.reversal_engine import stats_repo
    db = repo.get_db()
    db.run("INSERT INTO re_signals (created_at, signal_ref, direction, status, outcome, "
           "sl_dist, tp1, price_at_signal) VALUES (?,?,?,?,?,?,?,?)",
           _T0, "A", "BUY", "closed", "win", 6.0, 2102.0, 2100.0)
    db.run("INSERT INTO re_signals (created_at, signal_ref, direction, status, outcome) "
           "VALUES (?,?,?,?,?)", _T0, "B", "BUY", "pending", "open")
    return stats_repo.benchmark_rows()
