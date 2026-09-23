"""Recording cross-asset context for every signal. docs/todo/reversal-engine/230.

The sweep fills `re_signals.xasset_json` newest day first, then works back
through history one day per pass, reading M5 bars through the engine's own
bridge. Nothing here places, closes or modifies a trade: the only bridge
calls are candle reads, made against a fake.
"""
import asyncio
import json
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import cross_asset as xa
from backend.src.services.reversal_engine import reversal_engine_repo as repo
from backend.src.services.reversal_engine import xasset_repo
from backend.src.services.reversal_engine import xasset_sweep as sweep
from tests.conftest import remove_db_file

_DAY = 86_400.0
_T_TODAY = 1_790_150_000.0              # 2026-09-23
_T_OLD = _T_TODAY - 3 * _DAY


@pytest.fixture
def fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    sweep._skip_days.clear()
    yield repo
    repo.close_db()
    remove_db_file(path)
    sweep._skip_days.clear()


def _signal(ref, t, direction="BUY"):
    return repo.create_signal({"signal_ref": ref, "direction": direction, "created_at": t})


class _Bridge:
    """Serves a gently moving series for every symbol, and records calls."""

    def __init__(self, empty=(), fail_all_peers=False):
        self.calls = []
        self.empty = set(empty)
        self.fail_all_peers = fail_all_peers

    async def get_candles_range_for_symbol(self, symbol, from_ts, to_ts, timeframe="M5"):
        self.calls.append((symbol, from_ts, to_ts, timeframe))
        if symbol in self.empty:
            return []
        if self.fail_all_peers and symbol != xa.GOLD:
            return []
        start = int(from_ts // 300) * 300
        out, x = [], 100.0 + len(symbol)
        ts = start
        i = 0
        while ts + 300 <= to_ts:
            x *= 1 + (0.001 if (i + len(symbol)) % 3 else -0.0015)
            out.append({"ts": ts, "close": x})
            ts += 300
            i += 1
        return out


def _run(bridge):
    asyncio.run(sweep.xasset_sweep(None, bridge=bridge))


class TestWhatItFills:
    def test_it_fills_the_newest_day_first(self, fresh):
        old = _signal("old", _T_OLD)
        new = _signal("new", _T_TODAY)
        _run(_Bridge())
        assert repo.get_signal_by_id(new)["xasset_json"] is not None
        assert repo.get_signal_by_id(old)["xasset_json"] is None

    def test_the_next_pass_works_back_through_history(self, fresh):
        old = _signal("old", _T_OLD)
        _signal("new", _T_TODAY)
        _run(_Bridge())
        _run(_Bridge())
        assert repo.get_signal_by_id(old)["xasset_json"] is not None

    def test_the_stored_record_is_a_snapshot_at_the_signals_creation(self, fresh):
        sid = _signal("a", _T_TODAY, "SELL")
        _run(_Bridge())
        stored = json.loads(repo.get_signal_by_id(sid)["xasset_json"])
        assert stored["v"] == xa.SCHEMA_VERSION
        assert stored["t"] == _T_TODAY
        assert set(stored["features"]) == set(xa.FEATURE_NAMES)

    def test_it_reads_only_bars_before_the_newest_signal(self, fresh):
        _signal("a", _T_TODAY)
        b = _Bridge()
        _run(b)
        assert b.calls
        assert all(to_ts <= _T_TODAY for _s, _f, to_ts, _tf in b.calls)
        assert all(tf == "M5" for *_x, tf in b.calls)

    def test_a_filled_row_is_never_fetched_again(self, fresh):
        _signal("a", _T_TODAY)
        _run(_Bridge())
        b = _Bridge()
        _run(b)
        assert b.calls == []


class TestWhenItCannot:
    def test_no_bridge_is_a_no_op(self, fresh):
        sid = _signal("a", _T_TODAY)
        asyncio.run(sweep.xasset_sweep(None, bridge=None,
                                       bridge_getter=lambda: None))
        assert repo.get_signal_by_id(sid)["xasset_json"] is None

    def test_no_gold_history_marks_nothing_and_skips_the_day(self, fresh):
        """A day the broker has no gold for cannot be measured. It is skipped
        for this run rather than hammered every minute, and nothing is
        written that would read as a measurement."""
        sid = _signal("a", _T_TODAY)
        b = _Bridge(empty={xa.GOLD})
        _run(b)
        assert repo.get_signal_by_id(sid)["xasset_json"] is None
        b2 = _Bridge()
        _run(b2)
        assert b2.calls == []

    def test_every_peer_failing_is_a_bridge_problem_and_is_retried(self, fresh):
        sid = _signal("a", _T_TODAY)
        _run(_Bridge(fail_all_peers=True))
        assert repo.get_signal_by_id(sid)["xasset_json"] is None
        _run(_Bridge())
        assert repo.get_signal_by_id(sid)["xasset_json"] is not None

    def test_one_missing_peer_is_recorded_as_missing(self, fresh):
        sid = _signal("a", _T_TODAY)
        _run(_Bridge(empty={"VIX"}))
        stored = json.loads(repo.get_signal_by_id(sid)["xasset_json"])
        assert stored["features"]["VIX_corr"] is None
        assert stored["features"]["XAGUSD_corr"] is not None


class TestTheFitHistory:
    def test_a_fit_is_recorded_and_read_back_oldest_first(self, fresh):
        xasset_repo.record_fit(ts=2.0, n=300, auc_base=0.55, auc_xasset=0.58,
                               installed="xasset", per_peer={"XAGUSD": {"auc_z60": 0.52}})
        xasset_repo.record_fit(ts=1.0, n=290, auc_base=0.54, auc_xasset=None,
                               installed="base", per_peer={})
        rows = xasset_repo.fit_history()
        assert [r["ts"] for r in rows] == [1.0, 2.0]
        assert rows[1]["per_peer"]["XAGUSD"]["auc_z60"] == 0.52
        assert rows[0]["auc_xasset"] is None

    def test_stored_records_are_read_with_their_times(self, fresh):
        sid = _signal("a", _T_TODAY)
        xasset_repo.store_xasset([(sid, json.dumps({"v": 1, "features": {}}))])
        rows = xasset_repo.stored_records()
        assert rows == [{"created_at": _T_TODAY, "xasset_json": '{"v": 1, "features": {}}'}]


class TestItIsOnTheTimer:
    def test_the_research_loop_runs_the_sweep(self):
        from unittest import mock
        called = []

        async def _noop(engine):
            return None

        async def _sweep(engine):
            called.append(engine)

        engine = object()
        running = {"n": 0}

        def _is_running():
            running["n"] += 1
            return running["n"] <= 1

        loop = "backend.src.services.reversal_engine.research_loop"

        async def _go():
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
                 mock.patch(f"{loop}._reversal_engine_research_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._reversal_engine_study_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._breakout_excursion_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._meta_label_refit_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._xasset_sweep_impl", side_effect=_sweep):
                from backend.src.services.reversal_engine import research_loop
                await research_loop.reversal_engine_research_loop(engine, _is_running)

        asyncio.run(_go())
        assert called == [engine]
