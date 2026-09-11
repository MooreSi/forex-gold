"""The phase-1 study, composed.

docs/todo/reversal-engine/200: reconstruct excursion, measure execution
cost, attribute by cohort, fit barriers, sweep exit policies. Nothing in it
places, modifies or closes anything.

The property worth testing hardest is that it DEGRADES. A broker that no
longer serves ticks that far back, a bridge that is down, a sample too
small to fit on -- each must produce a report that says so, not an
exception halfway through. A research tool that raises is a research tool
nobody runs twice.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import research_lab as lab
from backend.src.services.reversal_engine import reversal_engine_repo as repo
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


class _DeadBridge:
    async def get_ticks_range(self, a, b):
        raise RuntimeError("bridge down")

    async def get_tick_at(self, ts):
        raise RuntimeError("bridge down")


class _EmptyBridge:
    async def get_ticks_range(self, a, b):
        return []

    async def get_tick_at(self, ts):
        return None


@pytest.fixture
def no_main_db(monkeypatch):
    """The study reads the main trading database for execution cost. These
    tests are about the reversal-engine half, so the cost step is stubbed
    to the shape it returns when nothing has been costed."""
    monkeypatch.setattr(lab.tca_repo, "trades_awaiting_cost_measurement",
                        lambda *a, **k: [])
    monkeypatch.setattr(lab.tca_repo, "mean_cost_r", lambda *a, **k: (None, 0))


class TestItDegrades:
    def test_a_dead_bridge_produces_a_report_not_an_exception(
            self, fresh_repo, no_main_db):
        report = asyncio.run(lab.run_study(_DeadBridge()))
        assert report["n_closed"] == 0
        assert report["backfill"]["measured"] == 0

    def test_an_empty_broker_history_is_reported_as_no_coverage(
            self, fresh_repo, no_main_db):
        sid = fresh_repo.create_signal({"signal_ref": "RE-1", "direction": "BUY"})
        repo.get_db().run(
            "UPDATE re_signals SET status='closed', outcome='loss', "
            "live_exec_status='executed', trigger_price=3300.0, "
            "trigger_time=1000.0, close_time=1100.0 WHERE id=?", sid)

        report = asyncio.run(lab.run_study(_EmptyBridge()))
        assert report["backfill"]["no_coverage"] == 1
        assert report["backfill"]["measured"] == 0

    def test_too_small_a_sample_refuses_to_fit_a_barrier(
            self, fresh_repo, no_main_db):
        report = asyncio.run(lab.run_study(_EmptyBridge()))
        assert report["barrier_fit"]["stop_pts"] is None
        assert "sample" in report["barrier_fit"]["refusal"].lower()

    def test_the_rendered_report_says_what_it_could_not_do(
            self, fresh_repo, no_main_db):
        text = lab.render(asyncio.run(lab.run_study(_EmptyBridge())))
        assert "not yet measurable" in text
        assert "sample" in text.lower()


class TestItMeasuresWhenItCan:
    def test_a_trade_with_tick_history_gets_an_excursion_and_a_path(
            self, fresh_repo, no_main_db):
        sid = fresh_repo.create_signal({"signal_ref": "RE-2", "direction": "BUY"})
        repo.get_db().run(
            "UPDATE re_signals SET status='closed', outcome='win', "
            "live_exec_status='executed', trigger_price=3300.0, "
            "trigger_time=1000.0, close_time=1005.0, sl_dist=5.0, "
            "pnl_pts=2.0, net_pnl_dollars=20.0, level_type='round_5', "
            "session='london' WHERE id=?", sid)

        class _Bridge(_EmptyBridge):
            async def get_ticks_range(self, a, b):
                return [{"time": 1000.0 + i, "bid": p, "ask": p + 0.4}
                        for i, p in enumerate([3300.0, 3304.0, 3299.0])]

        report = asyncio.run(lab.run_study(_Bridge()))
        assert report["backfill"]["measured"] == 1
        assert repo.get_signal_by_id(sid)["mfe_pts"] == pytest.approx(4.0)
        assert report["n_paths"] == 1

    def test_the_attribution_table_reaches_the_report(
            self, fresh_repo, no_main_db):
        sid = fresh_repo.create_signal({"signal_ref": "RE-3", "direction": "BUY"})
        repo.get_db().run(
            "UPDATE re_signals SET status='closed', outcome='win', "
            "live_exec_status='executed', trigger_price=3300.0, "
            "trigger_time=1000.0, close_time=1005.0, sl_dist=5.0, "
            "pnl_pts=2.0, net_pnl_dollars=20.0, level_type='pdh', "
            "session='london' WHERE id=?", sid)
        report = asyncio.run(lab.run_study(_EmptyBridge()))
        assert "pdh" in report["attribution"]
