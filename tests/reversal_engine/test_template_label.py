"""The label a model should learn from: the trade the EA template places.

docs/todo/reversal-engine/240. Every triggered signal is replayed from its
real trigger price through the live template's exits ("30 TP1 SL50 and
Trail") on M1 bars, and the R stored in `re_signals.tpl_r`. The engine's own
virtual outcome is a different trade: its stop sits ~6.8 points from the
fill and its TP1 ~2.1, where the template's are 5 and 4.

Nothing here reaches a broker: the bridge is a fake serving candles.
"""
import asyncio
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import entry_study as es
from backend.src.services.reversal_engine import reversal_engine_repo as repo
from backend.src.services.reversal_engine import tpl_label
from tests.conftest import remove_db_file

T0 = 1_790_100_000.0 - (1_790_100_000.0 % 60)
COST_R = tpl_label.COST_PTS / 5.0


def _bars(path, start=T0):
    """Bars from a list of (high, low) pairs, one minute apart."""
    return [es.Bar(start + 60 * i, (h + l) / 2, h, l, (h + l) / 2)
            for i, (h, l) in enumerate(path)]


class TestTheLabel:
    def test_a_straight_stop_out_is_minus_one_r_and_the_cost(self):
        bars = _bars([(2001, 2000.5), (2000.8, 1994.0)])
        r = tpl_label.label_from_bars(bars, "BUY", 2001.0, T0 + 20)
        assert r == pytest.approx(-1.0 - COST_R)

    def test_a_run_to_ten_points_banks_the_partial_and_the_runner(self):
        bars = _bars([(2001, 2000.5), (2006, 2000.8), (2012, 2005.5)])
        r = tpl_label.label_from_bars(bars, "BUY", 2001.0, T0 + 20)
        # 55% at +4, the rest at +10, on a 5-point stop.
        assert r == pytest.approx((0.55 * 4 + 0.45 * 10) / 5 - COST_R)

    def test_a_sell_mirrors_it(self):
        bars = _bars([(2000.5, 1999.0), (2006.5, 1999.2)])
        r = tpl_label.label_from_bars(bars, "SELL", 2001.0, T0 + 20)
        assert r == pytest.approx(-1.0 - COST_R)

    def test_a_high_printed_before_the_trigger_is_not_credited(self):
        # The trigger bar reached 2012 before price fell to the fill at
        # 2001, then price drifts at the fill: no target was reached after
        # entry, so this is not a win.
        bars = _bars([(2012, 2000.8)] + [(2001.5, 2000.5)] * 30)
        r = tpl_label.label_from_bars(bars, "BUY", 2001.0, T0 + 20)
        assert r < 0.5

    def test_no_bar_at_or_before_the_trigger_is_no_label(self):
        bars = _bars([(2001, 2000)], start=T0 + 600)
        assert tpl_label.label_from_bars(bars, "BUY", 2001.0, T0) is None

    def test_a_gap_in_the_bars_at_the_trigger_is_no_label(self):
        # The last bar before the trigger is ten minutes older than it: the
        # minute the trade was entered in is missing, so the replay would
        # walk a path that skipped the entry.
        bars = _bars([(2001, 2000.5)]) + _bars([(2001, 1990)], start=T0 + 660)
        assert tpl_label.label_from_bars(bars, "BUY", 2001.0, T0 + 600) is None

    def test_no_bars_is_no_label(self):
        assert tpl_label.label_from_bars([], "BUY", 2001.0, T0) is None


# ── The sweep ────────────────────────────────────────────────────────────────

_DAY = 86_400.0
_NOW = T0 + 10 * _DAY


@pytest.fixture
def fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    tpl_label._skip_days.clear()
    yield repo
    repo.close_db()
    remove_db_file(path)
    tpl_label._skip_days.clear()


def _triggered(ref, t, direction="BUY", price=2001.0, status="closed"):
    sid = repo.create_signal({"signal_ref": ref, "direction": direction, "created_at": t - 60})
    repo.get_db().run("UPDATE re_signals SET status=?, trigger_price=?, trigger_time=? WHERE id=?",
                      status, price, t, sid)
    return sid


class _Bridge:
    """Serves a gentle rise from 2001, and records every call. Candle reads
    only: it has no order method to call."""

    def __init__(self, empty=False):
        self.calls = []
        self.empty = empty

    async def get_candles_range(self, from_ts, to_ts, timeframe="M1"):
        self.calls.append((from_ts, to_ts, timeframe))
        if self.empty:
            return []
        out, ts, px = [], int(from_ts // 60) * 60, 2001.0
        while ts <= to_ts:
            out.append({"ts": ts, "open": px, "high": px + 0.3, "low": px - 0.2, "close": px + 0.1})
            px += 0.1
            ts += 60
        return out


def _run(bridge, now=_NOW):
    asyncio.run(tpl_label.tpl_label_sweep(None, bridge=bridge, now=now))


class TestTheSweep:
    def test_it_labels_the_newest_day_first(self, fresh):
        old = _triggered("old", T0)
        new = _triggered("new", T0 + 3 * _DAY)
        _run(_Bridge())
        assert repo.get_signal_by_id(new)["tpl_r"] is not None
        assert repo.get_signal_by_id(old)["tpl_r"] is None

    def test_the_next_pass_works_back_through_history(self, fresh):
        old = _triggered("old", T0)
        _triggered("new", T0 + 3 * _DAY)
        _run(_Bridge())
        _run(_Bridge())
        assert repo.get_signal_by_id(old)["tpl_r"] is not None

    def test_a_trade_younger_than_the_replay_horizon_waits(self, fresh):
        young = _triggered("young", _NOW - 3600)
        _run(_Bridge())
        assert repo.get_signal_by_id(young)["tpl_r"] is None

    def test_a_signal_that_never_triggered_is_never_labelled(self, fresh):
        sid = repo.create_signal({"signal_ref": "exp", "direction": "BUY", "created_at": T0})
        repo.get_db().run("UPDATE re_signals SET status='expired' WHERE id=?", sid)
        bridge = _Bridge()
        _run(bridge)
        assert bridge.calls == []

    def test_it_reads_gold_m1_bars_covering_the_whole_horizon(self, fresh):
        _triggered("a", T0)
        bridge = _Bridge()
        _run(bridge)
        (lo, hi, tf), = bridge.calls
        assert tf == "M1"
        assert lo <= T0 - 60
        assert hi >= T0 + es.HORIZON_S

    def test_a_day_the_broker_has_no_bars_for_is_not_asked_for_again(self, fresh):
        _triggered("a", T0)
        bridge = _Bridge(empty=True)
        _run(bridge)
        _run(bridge)
        assert len(bridge.calls) == 1

    def test_no_bridge_is_a_quiet_no_op(self, fresh):
        sid = _triggered("a", T0)
        asyncio.run(tpl_label.tpl_label_sweep(None, bridge=None, now=_NOW))
        assert repo.get_signal_by_id(sid)["tpl_r"] is None

    def test_the_count_the_refit_watches_follows_the_labels(self, fresh):
        from backend.src.services.reversal_engine import tpl_label_repo
        _triggered("a", T0)
        _triggered("b", T0 + 60)
        assert tpl_label_repo.count_labelled() == 0
        _run(_Bridge())
        assert tpl_label_repo.count_labelled() == 2
