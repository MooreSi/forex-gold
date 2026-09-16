"""Excursion measurement for the breakout engine, which has never had any.

`bo_signals` has carried no `mfe_pts`/`mae_pts` since the engine was written,
so there is no reach distribution for it and no evidence base for choosing
its targets. `tp1_mult` has sat at 1.0 — a guess — across 122 closed trades
and -$1,248, and `rr_tp1` is 1.0 on every one of them. Break-even at the
realised payoff needs a 43.5% win rate; the engine runs 36.1%.

The Reversal Engine solved exactly this in 2026-09-11 with
`reversal_engine/excursion_backfill.py`, reconstructing MFE/MAE from broker
tick history rather than waiting months for it to accumulate forward. This is
the same mechanism for the other engine, and the rules pinned here are the
ones that decide whether the resulting numbers can be trusted:

  * a virtual signal's path is whatever the engine imagined, so only real
    fills are measured;
  * an open trade's path is not finished;
  * a row that already carries an excursion is never overwritten.

**It places nothing, closes nothing and modifies no order.** It writes one
column pair on closed history.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from backend.src.services.breakout_signal import excursion_backfill as bf
from backend.src.services.breakout_signal import breakout_signal_repo as repo
from backend.src.services.breakout_signal import measure_repo as mrepo
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


class _Bridge:
    def __init__(self, ticks=(), fail=False):
        self.ticks = list(ticks)
        self.calls: list[tuple[float, float]] = []
        self.fail = fail

    async def get_ticks_range(self, from_ts, to_ts):
        self.calls.append((from_ts, to_ts))
        if self.fail:
            raise RuntimeError("bridge down")
        return [t for t in self.ticks if from_ts <= t["time"] <= to_ts]


def _ticks(prices, start=1000.0, spread=0.4):
    return [{"time": start + i, "bid": p, "ask": p + spread}
            for i, p in enumerate(prices)]


# "success" is what breakout_signal_live_execute writes when the order
# reached the broker, and what breakout_signal_service's two closure-sync
# reads require. It is spelled out here, not imported from the repo module,
# on purpose: this file's job is to hold the value the ENGINE writes, and a
# test that imports the same constant the query uses only proves the two
# agree with each other -- which is exactly what let docs/todo/bugs/062
# through twelve mutants and a full checks run.
EXECUTED = "success"
# A real value from the live table: 123 of its 124 rows carry it.
NOT_EXECUTED = "skipped:live_off"


def _executed(fresh_repo, direction="BUY", trigger=3300.0,
              trigger_time=1000.0, close_time=1010.0, status="closed",
              exec_status=EXECUTED):
    sig_id = fresh_repo.create_signal({
        "signal_ref": f"BO-{direction}-{trigger_time}", "direction": direction,
        "breakout_type": "sweep", "entry_mid": trigger, "stop_loss": trigger - 5,
        "sl_dist": 5.0,
    })
    repo.get_db().run(
        "UPDATE bo_signals SET status=?, outcome='loss', live_exec_status=?, "
        "trigger_price=?, trigger_time=?, close_time=?, close_price=? WHERE id=?",
        status, exec_status, trigger, trigger_time, close_time,
        trigger - 5.0, sig_id)
    return sig_id


class TestTheColumnsExist:
    def test_the_migration_adds_the_excursion_columns(self, fresh_repo):
        cols = {r[1] for r in repo.get_db().all("PRAGMA table_info(bo_signals)")}
        assert {"mfe_pts", "mae_pts", "excursion_source"} <= cols


class TestWhichSignalsQualify:
    def test_an_executed_closed_signal_with_no_excursion_is_picked_up(self, fresh_repo):
        sid = _executed(fresh_repo)
        assert [r["id"] for r in mrepo.signals_awaiting_excursion_backfill()] == [sid]

    def test_a_signal_that_never_executed_is_not_backfilled(self, fresh_repo):
        """A virtual signal's path is whatever the engine imagined. Pooling
        those with real fills produces a distribution describing a population
        that never traded."""
        _executed(fresh_repo, exec_status=NOT_EXECUTED)
        assert mrepo.signals_awaiting_excursion_backfill() == []

    def test_the_reversal_engines_word_for_it_does_not_count_here(self, fresh_repo):
        """'executed' is what re_signals carries; bo_signals has never held
        it. The filter said 'executed' for a day, so it matched none of the
        124 stored signals and would have matched none of the live ones
        either (docs/todo/bugs/062)."""
        _executed(fresh_repo, exec_status="executed")
        assert mrepo.signals_awaiting_excursion_backfill() == []

    def test_an_open_signal_is_not_backfilled(self, fresh_repo):
        _executed(fresh_repo, status="triggered")
        assert mrepo.signals_awaiting_excursion_backfill() == []

    def test_a_row_that_already_has_one_is_left_alone(self, fresh_repo):
        sid = _executed(fresh_repo)
        mrepo.record_backfilled_excursion(sid, 3.0, 1.0)
        assert mrepo.signals_awaiting_excursion_backfill() == []


class TestTheWriteItselfRefusesToOverwrite:
    """The selection query is not the guard, it is the first of two.

    A mutant that stripped `AND mfe_pts IS NULL` from the UPDATE survived the
    whole file: every test asserted on which rows get PICKED, and none on
    what happens if the write is reached twice. That clause is the last line
    of defence for the live sampler's own watermarks, which are the only
    independent check on whether a reconstruction is accurate.
    """

    def test_a_second_write_does_not_change_the_first(self, fresh_repo):
        sid = _executed(fresh_repo)
        mrepo.record_backfilled_excursion(sid, 3.0, 1.0)
        mrepo.record_backfilled_excursion(sid, 99.0, 99.0)
        row = repo.get_db().get(
            "SELECT mfe_pts, mae_pts FROM bo_signals WHERE id=?", sid)
        assert (row["mfe_pts"], row["mae_pts"]) == (3.0, 1.0)

    def test_the_source_is_not_rewritten_either(self, fresh_repo):
        sid = _executed(fresh_repo)
        mrepo.record_backfilled_excursion(sid, 3.0, 1.0, source="live")
        mrepo.record_backfilled_excursion(sid, 9.0, 9.0, source="ticks")
        row = repo.get_db().get(
            "SELECT excursion_source FROM bo_signals WHERE id=?", sid)
        assert row["excursion_source"] == "live"


class TestWhatItMeasures:
    def test_a_buy_records_the_high_water_and_the_low_water(self, fresh_repo):
        sid = _executed(fresh_repo, direction="BUY", trigger=3300.0)
        bridge = _Bridge(_ticks([3300.0, 3303.0, 3297.0, 3301.0]))
        asyncio.run(bf.backfill(bridge))
        row = repo.get_db().get("SELECT mfe_pts, mae_pts FROM bo_signals WHERE id=?", sid)
        assert row["mfe_pts"] > 0 and row["mae_pts"] > 0

    def test_a_sell_measures_the_opposite_direction(self, fresh_repo):
        """Favourable for a SELL is DOWN. Getting this backwards would invert
        every reach number the targets are then chosen from."""
        sid = _executed(fresh_repo, direction="SELL", trigger=3300.0)
        bridge = _Bridge(_ticks([3300.0, 3290.0, 3302.0]))
        asyncio.run(bf.backfill(bridge))
        row = repo.get_db().get("SELECT mfe_pts, mae_pts FROM bo_signals WHERE id=?", sid)
        assert row["mfe_pts"] >= 9.0

    def test_it_records_where_the_number_came_from(self, fresh_repo):
        sid = _executed(fresh_repo)
        asyncio.run(bf.backfill(_Bridge(_ticks([3300.0, 3302.0]))))
        row = repo.get_db().get("SELECT excursion_source FROM bo_signals WHERE id=?", sid)
        assert row["excursion_source"]

    def test_it_does_not_look_past_the_close(self, fresh_repo):
        """What price did after an early exit is not part of that trade's
        path, and counting it would measure a trade nobody held."""
        _executed(fresh_repo, trigger_time=1000.0, close_time=1005.0)
        bridge = _Bridge(_ticks([3300.0] * 60))
        asyncio.run(bf.backfill(bridge))
        assert all(b <= 1005.0 for _a, b in bridge.calls)


class TestItDegradesRatherThanFailing:
    def test_a_bridge_that_throws_is_reported_not_raised(self, fresh_repo):
        _executed(fresh_repo)
        report = asyncio.run(bf.backfill(_Bridge(fail=True)))
        assert report.failed == 1
        assert report.errors

    def test_no_ticks_is_recorded_as_no_coverage_not_as_a_zero_excursion(self, fresh_repo):
        """A zero excursion and an unmeasurable one are different facts.
        Writing 0.0 would put a trade that never moved and a trade nobody has
        data for into the same bucket of the fit."""
        sid = _executed(fresh_repo)
        report = asyncio.run(bf.backfill(_Bridge([])))
        assert report.no_coverage == 1
        row = repo.get_db().get("SELECT mfe_pts FROM bo_signals WHERE id=?", sid)
        assert row["mfe_pts"] is None

    def test_a_row_with_no_usable_window_is_skipped(self, fresh_repo):
        sid = _executed(fresh_repo)
        repo.get_db().run("UPDATE bo_signals SET trigger_time=0 WHERE id=?", sid)
        report = asyncio.run(bf.backfill(_Bridge(_ticks([3300.0]))))
        assert report.skipped_no_window == 1


class TestWhatItFeeds:
    def test_observations_come_back_for_the_barrier_fit(self, fresh_repo):
        """The whole point: a population `market/barrier_fit` can fit on, so
        the breakout engine's targets can be chosen from measurement instead
        of from the 1.0 that has been there since it was written."""
        _executed(fresh_repo)
        asyncio.run(bf.backfill(_Bridge(_ticks([3300.0, 3305.0, 3298.0]))))
        obs = mrepo.excursion_observations()
        assert len(obs) == 1
        assert obs[0]["mfe_pts"] is not None and obs[0]["sl_dist"]

    def test_a_virtual_signal_never_reaches_the_fit(self, fresh_repo):
        """A mutant that dropped `live_exec_status='executed'` from the
        observations query survived this whole file, because every test here
        created executed signals only. The filter is the entire reason these
        numbers can be trusted: a virtual signal's path is whatever the engine
        imagined, and pooling those with real fills produces a reach
        distribution describing a population that never traded -- which is
        then what the targets get chosen from.
        """
        real = _executed(fresh_repo, trigger_time=1000.0)
        ghost = _executed(fresh_repo, trigger_time=2000.0,
                          exec_status=NOT_EXECUTED)
        for sid in (real, ghost):
            mrepo.record_backfilled_excursion(sid, 4.0, 2.0)
        obs = mrepo.excursion_observations()
        assert len(obs) == 1

    def test_coverage_reports_how_many_are_measured(self, fresh_repo):
        _executed(fresh_repo)
        asyncio.run(bf.backfill(_Bridge(_ticks([3300.0, 3305.0]))))
        assert sum(mrepo.excursion_coverage().values()) == 1

    def test_coverage_counts_only_what_actually_executed(self, fresh_repo):
        """The third query carried the same wrong status word and nothing
        held it to a real one, so it reported coverage over a population it
        could never select."""
        measured = _executed(fresh_repo, trigger_time=1000.0)
        skipped = _executed(fresh_repo, trigger_time=2000.0,
                            exec_status=NOT_EXECUTED)
        for sid in (measured, skipped):
            mrepo.record_backfilled_excursion(sid, 4.0, 2.0)
        assert sum(mrepo.excursion_coverage().values()) == 1
