"""The virtual trade ledger answers one question per row (2026-10-02).

The owner saw the same dollar figure many times over ("-$558.40") and read it
as invented. It was not: every signal writes one row per variant, all pointing
at the same signal's result, and four of the five variants could never differ
because each also carried the champion's ML floor. Three separate faults, one
test each below:

  1. a variant that cannot differ from the champion measures nothing
  2. the same trade's dollars repeated per variant look like many trades
  3. R read from the last leg's points calls a $20 ladder win "0.02R"
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as repo
from backend.src.services.reversal_engine import shadow
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


def _closed(ref, net, pnl_pts=0.1, sl_dist=7.0, live_exec=None, tpl_r=None,
            outcome="win", created_at=1000.0):
    repo.get_db().run(
        "INSERT INTO re_signals (created_at, signal_ref, direction, sl_dist, "
        " status, outcome, pnl_pts, net_pnl_dollars, live_exec_status, tpl_r) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        created_at, ref, "BUY", sl_dist, "closed", outcome, pnl_pts, net,
        live_exec, tpl_r)


def ctx(ml_prob=0.5, meta_prob=None, trigger_passed=True, liquidity_blocked=False):
    return {"ml_prob": ml_prob, "meta_prob": meta_prob,
            "trigger_passed": trigger_passed,
            "liquidity_blocked": liquidity_blocked}


def _variant(name):
    return next(v for v in shadow.DEFAULT_VARIANTS if v.name == name)


class TestVariantsCanDiffer:

    def test_a_challenger_does_not_inherit_the_champions_ml_floor(self):
        # Negative predicted R: the champion stands aside. A trigger-only
        # variant has no ML opinion, so it takes the trade. Before, all four
        # challengers also refused here, and so were identical to the champion
        # on every signal the ML gate skipped.
        champion = next(v for v in shadow.DEFAULT_VARIANTS if v.is_champion)
        others = [v for v in shadow.DEFAULT_VARIANTS
                  if not v.is_champion and v.min_ml_prob is None]
        assert shadow.decide(champion, ctx(ml_prob=-0.4))[0] is False
        assert others, "no challenger measures a gate on its own"
        assert all(shadow.decide(v, ctx(ml_prob=-0.4))[0] is True for v in others)

    def test_every_default_variant_can_disagree_with_the_champion(self):
        champion = next(v for v in shadow.DEFAULT_VARIANTS if v.is_champion)
        probes = [ctx(ml_prob=-0.4), ctx(ml_prob=0.9, trigger_passed=False),
                  ctx(ml_prob=0.9, liquidity_blocked=True),
                  ctx(ml_prob=0.9, meta_prob=0.1), ctx(ml_prob=0.2)]
        for v in shadow.DEFAULT_VARIANTS:
            if v.is_champion:
                continue
            assert any(shadow.decide(v, c)[0] != shadow.decide(champion, c)[0]
                       for c in probes), f"{v.name} can never differ"

    def test_a_refusal_reports_every_reason_not_only_the_first(self):
        v = shadow.Variant("all", min_ml_prob=0.0, require_trigger=True,
                           respect_liquidity=True)
        take, reason = shadow.decide(
            v, ctx(ml_prob=-1.0, trigger_passed=False, liquidity_blocked=True))
        assert take is False
        assert "ML" in reason and "not confirmed" in reason and "liquidity" in reason


class TestRFromTheWholeTrade:

    def test_a_ladder_win_is_scored_on_what_it_banked_not_its_last_leg(
            self, fresh_repo):
        # +$21 on a 7pt stop at 0.1 lot is 0.30R. The last leg moved 0.1pt.
        _closed("RE-w", net=21.0, pnl_pts=0.1, sl_dist=7.0)
        shadow.record_all("RE-w", ctx())
        row = next(r for r in shadow.history(50) if r["signal_ref"] == "RE-w")
        assert row["r_full"] == pytest.approx(21.0 / 7.0 / 10.0)
        assert row["r"] == pytest.approx(0.1 / 7.0)      # the legacy figure, unchanged

    def test_a_broker_executed_trade_has_no_full_r_rather_than_a_wrong_one(
            self, fresh_repo):
        # Its dollars are at the real lot, so net / (sl * 0.1 lot) is not R.
        _closed("RE-x", net=-50.0, sl_dist=7.0, live_exec="executed")
        shadow.record_all("RE-x", ctx())
        row = next(r for r in shadow.history(50) if r["signal_ref"] == "RE-x")
        assert row["r_full"] is None

    def test_the_replay_r_rides_along_so_the_two_methods_can_be_compared(
            self, fresh_repo):
        _closed("RE-y", net=21.0, tpl_r=0.415)
        shadow.record_all("RE-y", ctx())
        row = next(r for r in shadow.history(50) if r["signal_ref"] == "RE-y")
        assert row["r_replay"] == pytest.approx(0.415)


class TestOneRowPerSignal:

    def test_the_ledger_has_one_row_per_signal_with_every_variants_call(
            self, fresh_repo):
        _closed("RE-a", net=-30.0, outcome="loss")
        shadow.record_all("RE-a", ctx(ml_prob=-0.5))
        rows = shadow.ledger(20)
        assert len(rows) == 1
        calls = rows[0]["calls"]
        assert set(calls) == {v.name for v in shadow.DEFAULT_VARIANTS}
        assert calls["live (champion)"]["take"] is False
        assert "ML" in calls["live (champion)"]["reason"]

    def test_the_trades_dollars_appear_once_on_the_row(self, fresh_repo):
        _closed("RE-a", net=-558.4, outcome="loss")
        shadow.record_all("RE-a", ctx())
        (row,) = shadow.ledger(20)
        assert row["net"] == pytest.approx(-558.4)
        assert "net" not in next(iter(row["calls"].values()))

    def test_a_row_for_an_unsettled_signal_has_no_result_not_zero(self, fresh_repo):
        repo.get_db().run(
            "INSERT INTO re_signals (created_at, signal_ref, direction, sl_dist, "
            " status, outcome) VALUES (1000.0,'RE-p','BUY',7.0,'pending','open')")
        shadow.record_all("RE-p", ctx())
        (row,) = shadow.ledger(20)
        assert row["net"] is None and row["r_full"] is None

    def test_decisions_by_a_retired_variant_are_not_shown(self, fresh_repo):
        _closed("RE-a", net=1.0)
        shadow.record_all("RE-a", ctx())
        repo.get_db().run(
            "INSERT INTO re_shadow_decisions (ts, signal_ref, variant, would_take, "
            "reason) VALUES (1,'RE-a','confirmed entries',1,'')")
        (row,) = shadow.ledger(20)
        assert "confirmed entries" not in row["calls"]
        assert all(d["variant"] != "confirmed entries" for d in shadow.history(50))

    def test_the_limit_counts_signals_not_decision_rows(self, fresh_repo):
        for i in range(5):
            _closed(f"RE-{i}", net=1.0, created_at=1000.0 + i)
            shadow.record_all(f"RE-{i}", ctx())
        assert len(shadow.ledger(3)) == 3


class TestTheReport:

    def test_a_variant_reports_the_result_of_the_signals_it_skipped(
            self, fresh_repo):
        # A skip on a loser is the variant being right: the avoided loss is the
        # evidence, and "taken" alone cannot show it.
        _closed("RE-loss", net=-40.0, outcome="loss")
        _closed("RE-win", net=10.0)
        shadow.record_all("RE-loss", ctx(ml_prob=-0.5))
        shadow.record_all("RE-win", ctx(ml_prob=0.5))
        champ = next(r for r in shadow.report() if r["is_champion"])
        assert champ["net"] == pytest.approx(10.0)
        assert champ["avoided_net"] == pytest.approx(-40.0)

    def test_a_variant_is_compared_with_the_champion_not_just_summed(
            self, fresh_repo):
        _closed("RE-loss", net=-40.0, outcome="loss")
        shadow.record_all("RE-loss", ctx(ml_prob=-0.5))
        rows = {r["variant"]: r for r in shadow.report()}
        no_filter = rows["no filter"]
        assert no_filter["net"] == pytest.approx(-40.0)
        assert no_filter["delta_vs_champion"] == pytest.approx(-40.0)
        assert rows["live (champion)"]["delta_vs_champion"] == pytest.approx(0.0)

    def test_the_mean_r_on_the_whole_trade_is_reported_beside_the_legacy_one(
            self, fresh_repo):
        _closed("RE-w", net=21.0, pnl_pts=0.1, sl_dist=7.0)
        shadow.record_all("RE-w", ctx())
        champ = next(r for r in shadow.report() if r["is_champion"])
        assert champ["mean_r_full"] == pytest.approx(0.3)
        assert champ["mean_net"] == pytest.approx(21.0)

    def test_a_variant_with_nothing_to_score_reports_none_not_zero(self, fresh_repo):
        assert all(r["mean_r_full"] is None and r["mean_net"] is None
                   for r in shadow.report())
