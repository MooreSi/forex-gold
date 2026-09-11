"""Reconstruct MFE/MAE for signals that closed before the live path ever
measured itself.

docs/todo/reversal-engine/200 section 1.2. Items
[020](../../docs/todo/reversal-engine/020-losses-exceed-the-stop.md) and
[030](../../docs/todo/reversal-engine/030-wins-are-cut-at-two-thirds-of-a-r.md)
are both blocked waiting for excursion data to accumulate forward at about
nine signals a day. The broker already holds the tick history those trades
walked through, and `bridge.get_ticks_range` has been wired end to end since
before either item was raised.

The rules pinned here are the ones that decide whether the backfilled numbers
can be trusted alongside the live-sampled ones.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import excursion_backfill as bf
from backend.src.services.reversal_engine import measure_repo as mr
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


class _Bridge:
    """Serves ticks from a fixed list, clipped to the requested window, and
    records every window it was asked for."""

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


def _executed_signal(fresh_repo, direction="BUY", trigger=3300.0,
                     trigger_time=1000.0, close_time=1010.0):
    sig_id = fresh_repo.create_signal({
        "signal_ref": f"RE-{direction}-{trigger_time}", "direction": direction,
        "entry_low": trigger - 1, "entry_high": trigger + 1, "sl_dist": 5.0,
    })
    repo.get_db().run(
        "UPDATE re_signals SET status='closed', outcome='loss', "
        "live_exec_status='executed', trigger_price=?, trigger_time=?, "
        "close_time=?, close_price=? WHERE id=?",
        trigger, trigger_time, close_time, trigger - 5.0, sig_id)
    return sig_id


class TestWhichSignalsQualify:
    def test_an_executed_closed_signal_with_no_excursion_is_picked_up(self, fresh_repo):
        sid = _executed_signal(fresh_repo)
        assert [r["id"] for r in mr.signals_awaiting_excursion_backfill()] == [sid]

    def test_a_signal_that_already_has_an_excursion_is_left_alone(self, fresh_repo):
        """The live path's own watermarks are not overwritten. They were
        sampled on a real five-second tick against the real stop, and a
        backfill that silently replaced them would destroy the only
        independent check on its own accuracy."""
        sid = _executed_signal(fresh_repo)
        mr.record_excursion(sid, 3.0, 1.0)
        assert mr.signals_awaiting_excursion_backfill() == []

    def test_a_signal_that_never_executed_is_not_backfilled(self, fresh_repo):
        """A virtual signal's "path" is whatever the engine imagined. Mixing
        those into the same column as real fills is how a fit ends up
        describing a population that never traded."""
        sid = _executed_signal(fresh_repo)
        repo.get_db().run("UPDATE re_signals SET live_exec_status='skipped:ml' "
                          "WHERE id=?", sid)
        assert mr.signals_awaiting_excursion_backfill() == []

    def test_an_open_signal_is_not_backfilled(self, fresh_repo):
        sid = _executed_signal(fresh_repo)
        repo.get_db().run("UPDATE re_signals SET status='triggered', "
                          "close_time=NULL WHERE id=?", sid)
        assert mr.signals_awaiting_excursion_backfill() == []


class TestTheReconstruction:
    def test_it_records_the_excursion_the_ticks_actually_show(self, fresh_repo):
        sid = _executed_signal(fresh_repo, trigger_time=1000.0, close_time=1004.0)
        bridge = _Bridge(_ticks([3300.0, 3303.0, 3297.5, 3301.0, 3299.0]))

        report = asyncio.run(bf.backfill(bridge))

        assert report.measured == 1
        row = repo.get_signal_by_id(sid)
        assert row["mfe_pts"] == pytest.approx(3.0)
        assert row["mae_pts"] == pytest.approx(2.5)

    def test_a_long_is_measured_on_the_bid_and_a_short_on_the_ask(self, fresh_repo):
        """Half a spread on every trade, in the same direction every time, is
        exactly the size of the leakage item 020 is chasing."""
        _executed_signal(fresh_repo, direction="SELL", trigger=3300.0,
                         trigger_time=1000.0, close_time=1002.0)
        bridge = _Bridge(_ticks([3300.0, 3296.0, 3300.0], spread=0.4))

        asyncio.run(bf.backfill(bridge))

        rows = repo.get_all_signals()
        # SELL entered at 3300 and closed at the ASK, which is bid + 0.4.
        assert rows[0]["mfe_pts"] == pytest.approx(3.6)
        assert rows[0]["mae_pts"] == pytest.approx(0.4)

    def test_the_window_is_the_trade_not_the_whole_day(self, fresh_repo):
        _executed_signal(fresh_repo, trigger_time=1000.0, close_time=1002.0)
        bridge = _Bridge(_ticks([3300.0, 3301.0, 3302.0, 3390.0, 3200.0]))

        asyncio.run(bf.backfill(bridge))

        rows = repo.get_all_signals()
        assert rows[0]["mfe_pts"] == pytest.approx(2.0)
        assert bridge.calls[0] == (1000.0, 1002.0)

    def test_a_long_window_is_fetched_in_bounded_chunks(self, fresh_repo):
        """`_get_ticks_range` refuses a span over `_MAX_TICKS_RANGE_SEC` and
        returns None rather than raising, so an unchunked request for a
        multi-day trade would silently record no coverage at all."""
        t0 = 1_700_000_000.0
        _executed_signal(fresh_repo, trigger_time=t0, close_time=t0 + 3 * 86400.0)
        bridge = _Bridge(_ticks([3300.0], start=t0))

        asyncio.run(bf.backfill(bridge))

        assert len(bridge.calls) == 3
        assert all(b - a <= 86400.0 for a, b in bridge.calls)


class TestItRefusesToInventNumbers:
    def test_no_ticks_means_no_row_written(self, fresh_repo):
        """Recording 0.0/0.0 for a trade with no tick coverage would be a
        fabricated observation, the same class of error as the fabricated
        $0.00 closes in reversal-engine/010, and it would drag every fitted
        barrier toward zero."""
        sid = _executed_signal(fresh_repo)
        report = asyncio.run(bf.backfill(_Bridge([])))

        assert report.measured == 0
        assert report.no_coverage == 1
        assert repo.get_signal_by_id(sid)["mfe_pts"] is None

    def test_a_bridge_failure_is_counted_not_swallowed_silently(self, fresh_repo):
        _executed_signal(fresh_repo)
        report = asyncio.run(bf.backfill(_Bridge(fail=True)))
        assert report.failed == 1
        assert report.measured == 0

    def test_a_signal_with_no_fill_price_is_skipped(self, fresh_repo):
        sid = _executed_signal(fresh_repo)
        repo.get_db().run("UPDATE re_signals SET trigger_price=0 WHERE id=?", sid)
        report = asyncio.run(bf.backfill(_Bridge(_ticks([3300.0, 3305.0]))))
        assert report.skipped_no_window == 1


class TestProvenance:
    def test_a_backfilled_row_says_it_was_backfilled(self, fresh_repo):
        """Live-sampled and tick-reconstructed excursions have different
        error characteristics -- the live sampler polls every five seconds
        and misses spikes between polls, the reconstruction does not. Mixing
        them without a label makes that difference unmeasurable afterwards."""
        sid = _executed_signal(fresh_repo, trigger_time=1000.0, close_time=1002.0)
        asyncio.run(bf.backfill(_Bridge(_ticks([3300.0, 3302.0, 3299.0]))))
        assert repo.get_signal_by_id(sid)["excursion_source"] == "ticks"

    def test_coverage_counts_by_source(self, fresh_repo):
        live = _executed_signal(fresh_repo, trigger_time=1000.0, close_time=1002.0)
        mr.record_excursion(live, 2.0, 1.0)
        _executed_signal(fresh_repo, trigger=3400.0, trigger_time=2000.0,
                         close_time=2002.0)
        asyncio.run(bf.backfill(_Bridge(
            [{"time": 2000.0 + i, "bid": p, "ask": p + 0.4}
             for i, p in enumerate([3400.0, 3402.0, 3399.0])])))

        cov = mr.excursion_coverage()
        assert cov["ticks"] == 1
        assert cov["live"] == 1
