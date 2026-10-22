"""Signals stored with a peer missing are measured again. reversal-engine/230.

Until 2026-10-22 the bridge read six of the seven peers from MT5's cache
(they were not in Market Watch), the staleness guard refused them, and each
signal was stored once with those peers missing. A measured row was never
selected again, so fixing the bridge alone would have left 7,600 signals'
history empty for good.

A day is measured again when some peer is missing on EVERY signal of it --
the signature of a feed that did not serve it, not of a market that was shut
for part of the day. It happens only once nothing is waiting for a first
measurement, at most once per day per run, and a row is replaced only by a
reading that measured more peers than the one it had.

Nothing here places, closes or modifies a trade: the only bridge calls are
candle reads, made against a fake.
"""
import asyncio
import json

import pytest

from backend.src.services.reversal_engine import cross_asset as xa
from backend.src.services.reversal_engine import xasset_repo
from backend.src.services.reversal_engine import xasset_sweep as sweep
from tests.reversal_engine.test_cross_asset_sweep import (  # noqa: F401
    _DAY, _T_OLD, _T_TODAY, _Bridge, _signal, fresh,
)


@pytest.fixture(autouse=True)
def _forget_passes():
    sweep._remeasured_days.clear()
    sweep._peer_misses.clear()
    yield
    sweep._remeasured_days.clear()
    sweep._peer_misses.clear()


def _run(bridge):
    asyncio.run(sweep.xasset_sweep(None, bridge=bridge))


def _features(repo, sid):
    return json.loads(repo.get_signal_by_id(sid)["xasset_json"])["features"]


def _store(sid, t, missing=()):
    feats = {name: (None if name.rsplit("_", 2)[0] in missing
                    or name.startswith(tuple(f"{p}_" for p in missing)) else 0.1)
             for name in xa.FEATURE_NAMES}
    xasset_repo.store_xasset([(sid, json.dumps({"v": 1, "t": t, "features": feats}))])


def test_a_peer_missing_all_day_is_measured_again_once_it_is_served(fresh):
    a = _signal("a", _T_TODAY)
    b = _signal("b", _T_TODAY - 600)
    _run(_Bridge(empty={"XAGUSD", "SP500"}))
    assert _features(fresh, a)["XAGUSD_corr"] is None

    _run(_Bridge())

    for sid in (a, b):
        f = _features(fresh, sid)
        assert f["XAGUSD_corr"] is not None
        assert f["SP500_corr"] is not None


def test_a_signal_never_measured_comes_first(fresh):
    old = _signal("old", _T_OLD)
    _run(_Bridge(empty={"XAGUSD"}))
    new = _signal("new", _T_TODAY)

    _run(_Bridge())

    assert fresh.get_signal_by_id(new)["xasset_json"] is not None
    assert _features(fresh, old)["XAGUSD_corr"] is None


def test_a_peer_missing_on_only_some_signals_is_a_shut_market_and_left(fresh):
    """VIX trades US hours. Missing on the night signal and present on the
    afternoon one is the market, not the feed."""
    # Same UTC day: _T_TODAY is 07:53.
    night = _signal("night", _T_TODAY - 2 * 3600)
    day = _signal("day", _T_TODAY)
    _store(night, _T_TODAY - 2 * 3600, missing=("VIX",))
    _store(day, _T_TODAY)

    b = _Bridge()
    _run(b)

    assert b.calls == []


def test_a_day_that_gains_nothing_is_tried_once_per_run(fresh):
    _signal("a", _T_TODAY)
    _run(_Bridge(empty={"XAGUSD"}))

    second = _Bridge(empty={"XAGUSD"})
    _run(second)
    third = _Bridge(empty={"XAGUSD"})
    _run(third)

    assert second.calls, "the first pass after measuring re-reads the day"
    assert third.calls == []


def test_a_reading_is_never_replaced_by_a_worse_one(fresh):
    sid = _signal("a", _T_TODAY)
    _store(sid, _T_TODAY, missing=("VIX",))
    before = fresh.get_signal_by_id(sid)["xasset_json"]

    _run(_Bridge(empty={"XAGUSD", "USDX"}))

    assert fresh.get_signal_by_id(sid)["xasset_json"] == before


def test_it_works_back_through_history_newest_day_first(fresh):
    old = _signal("old", _T_OLD)
    new = _signal("new", _T_TODAY)
    _run(_Bridge(empty={"XPTUSD"}))
    _run(_Bridge(empty={"XPTUSD"}))

    _run(_Bridge())
    assert _features(fresh, new)["XPTUSD_corr"] is not None
    assert _features(fresh, old)["XPTUSD_corr"] is None

    _run(_Bridge())
    assert _features(fresh, old)["XPTUSD_corr"] is not None
