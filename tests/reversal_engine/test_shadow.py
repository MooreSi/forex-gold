"""Champion and challenger: what the other configuration would have done.

Section 5.7 of docs/todo/reversal-engine/200. Today a version bump goes
live and the evidence arrives afterwards -- v9 shipped on a Saturday and
had its worst Asian session on the next trading day with nothing to compare
against.

A shadow decision is recorded from facts the live path has ALREADY
computed, not by re-running the gates against a second market read. Two
reasons: a second read is a different moment and would answer a different
question, and a shadow that makes its own bridge calls costs latency on the
live path it is shadowing.
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


def ctx(ml_prob=0.5, meta_prob=None, trigger_passed=True, liquidity_blocked=False):
    return {"ml_prob": ml_prob, "meta_prob": meta_prob,
            "trigger_passed": trigger_passed,
            "liquidity_blocked": liquidity_blocked}


class TestDeciding:
    def test_a_variant_takes_a_trade_that_clears_its_ml_floor(self):
        v = shadow.Variant("floor 0", min_ml_prob=0.0)
        assert shadow.decide(v, ctx(ml_prob=0.2))[0] is True

    def test_a_variant_refuses_a_trade_below_its_ml_floor(self):
        v = shadow.Variant("floor 0.3", min_ml_prob=0.3)
        take, reason = shadow.decide(v, ctx(ml_prob=0.2))
        assert take is False
        assert "ml" in reason.lower()

    def test_a_variant_that_requires_confirmation_refuses_an_unconfirmed_level(self):
        v = shadow.Variant("confirmed only", require_trigger=True)
        assert shadow.decide(v, ctx(trigger_passed=False))[0] is False

    def test_a_variant_ignoring_confirmation_takes_it_anyway(self):
        v = shadow.Variant("as now", require_trigger=False)
        assert shadow.decide(v, ctx(trigger_passed=False))[0] is True

    def test_an_unavailable_meta_probability_never_refuses(self):
        """No opinion is not a negative verdict. A challenger that blocked
        on a model which has not been fitted would report a refusal rate
        that says nothing about the model."""
        v = shadow.Variant("meta 0.6", meta_threshold=0.6)
        assert shadow.decide(v, ctx(meta_prob=None))[0] is True

    def test_a_liquidity_window_refuses_when_the_variant_respects_it(self):
        v = shadow.Variant("liquidity aware", respect_liquidity=True)
        assert shadow.decide(v, ctx(liquidity_blocked=True))[0] is False

    def test_the_default_variants_include_one_that_matches_today(self):
        """Without a champion in the list there is nothing to compare
        against, and a table of challengers alone proves nothing."""
        assert any(v.is_champion for v in shadow.DEFAULT_VARIANTS)


class TestRecording:
    def test_one_row_per_variant_per_signal(self, fresh_repo):
        shadow.record_all("RE-1", ctx())
        rows = shadow.decisions_for("RE-1")
        assert len(rows) == len(shadow.DEFAULT_VARIANTS)

    def test_recording_twice_does_not_double_count(self, fresh_repo):
        """A fill attempt can be retried. A shadow log that counted each
        retry would inflate whichever variant the retries happened to
        favour."""
        shadow.record_all("RE-1", ctx())
        shadow.record_all("RE-1", ctx())
        assert len(shadow.decisions_for("RE-1")) == len(shadow.DEFAULT_VARIANTS)

    def test_a_failure_to_record_never_reaches_the_caller(self, fresh_repo,
                                                          monkeypatch):
        """This sits on the live order path. A measurement must never cost a
        trade its execution."""
        monkeypatch.setattr(shadow, "_insert",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
        shadow.record_all("RE-1", ctx())


class TestTheReport:
    def _closed(self, fresh_repo, ref, pnl_pts, sl_dist=5.0, outcome="win"):
        sid = fresh_repo.create_signal({"signal_ref": ref, "direction": "BUY"})
        repo.get_db().run(
            "UPDATE re_signals SET status='closed', outcome=?, "
            "live_exec_status='executed', sl_dist=?, pnl_pts=?, "
            "net_pnl_dollars=? WHERE id=?",
            outcome, sl_dist, pnl_pts, pnl_pts * 10, sid)

    def test_each_variant_is_scored_only_on_the_trades_it_would_have_taken(
            self, fresh_repo):
        self._closed(fresh_repo, "RE-win", 5.0)
        self._closed(fresh_repo, "RE-loss", -5.0, outcome="loss")
        shadow.record_all("RE-win", ctx(ml_prob=0.9))
        shadow.record_all("RE-loss", ctx(ml_prob=0.1))

        rows = {r["variant"]: r for r in shadow.report()}
        selective = rows["ML floor 0.50"]
        assert selective["n_taken"] == 1
        assert selective["mean_r"] == pytest.approx(1.0)

    def test_the_champion_is_scored_on_everything_it_took(self, fresh_repo):
        self._closed(fresh_repo, "RE-win", 5.0)
        self._closed(fresh_repo, "RE-loss", -5.0, outcome="loss")
        shadow.record_all("RE-win", ctx(ml_prob=0.9))
        shadow.record_all("RE-loss", ctx(ml_prob=0.1))
        champ = next(r for r in shadow.report() if r["is_champion"])
        assert champ["n_taken"] == 2
        assert champ["mean_r"] == pytest.approx(0.0)

    def test_a_variant_with_no_decisions_yet_reports_no_edge_not_zero(
            self, fresh_repo):
        rows = shadow.report()
        assert all(r["mean_r"] is None for r in rows)
