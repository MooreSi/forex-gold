"""The Reversal Engine's Asia range is 00:00-07:59 UTC, on a UTC clock (2026-10-17).

`level_detector.get_asia_range` takes the session from each H1 bar's stamp,
and the live cycle handed it `get_candles("H1", 50)` as the bridge sends it:
broker time (New York 17:00 = 00:00) written as a UTC epoch. So live, the
"Asia" range was 21:00-04:59 UTC in the US summer and 22:00-05:59 in its
winter -- the New York close and the daily break in, Tokyo's last three hours
out -- while every replay of this engine (`historical_signals`, Dukascopy
bars, true UTC) measured the session the docstring describes. Live and its
own measurements were trading different levels.

No test here reaches a broker or a database.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.src.services.reversal_engine import cycle_setup
from backend.src.services.reversal_engine import level_detector as ld
from backend.src.services.reversal_engine import reversal_engine_service as svc
from backend.src.utils.models import Tick

H = 3600
OFF = 3 * H
# Wednesday 2026-10-14 10:20 UTC: today's Asia session (00-07 UTC) is complete.
NOW = datetime(2026, 10, 14, 10, 20, tzinfo=timezone.utc).timestamp()


def _broker_h1(n=50):
    """Broker-stamped H1 bars, the last (10:00 UTC) forming. Asia proper
    (00-07 UTC) trades 4040-4050; the 21:00-23:59 UTC bars the broker clock
    calls 00-02 reach 4100 and 3900."""
    last = datetime(2026, 10, 14, 10, tzinfo=timezone.utc).timestamp()
    out = []
    for k in range(n - 1, -1, -1):
        t = last - H * k
        h = datetime.fromtimestamp(t, timezone.utc).hour
        hi, lo = (4050.0, 4040.0) if h < 8 else (4100.0, 3900.0) if h >= 21 else (4020.0, 4010.0)
        out.append({"ts": int(t + OFF), "open": lo, "high": hi, "low": lo, "close": hi})
    return out


def _tick(age_s=2.0):
    return Tick(bid=4030.0, ask=4030.3, mid=4030.15, spread=0.3, spread_points=30.0,
                timestamp=NOW + OFF - age_s, source="test")


class TestUtcH1:
    def test_on_broker_stamps_the_range_is_the_wrong_hours(self):
        """The defect, reproduced on the real function: the New York close's
        extremes become the Asia range."""
        assert ld.get_asia_range(_broker_h1()) == (3900.0, 4100.0)

    def test_restamped_it_is_the_tokyo_session(self):
        h1 = cycle_setup.utc_h1(_broker_h1(), _tick(), NOW)
        assert ld.get_asia_range(h1) == (4040.0, 4050.0)

    def test_the_forming_bar_is_kept(self):
        """Bias, ATR and ADX read the same list; this fix moves the clock and
        nothing else."""
        assert len(cycle_setup.utc_h1(_broker_h1(), _tick(), NOW)) == 50

    def test_a_tick_too_stale_to_set_the_clock_leaves_the_bars_as_they_came(self):
        assert cycle_setup.utc_h1(_broker_h1(), _tick(age_s=40 * 60), NOW) == _broker_h1()

    def test_bars_already_in_utc_are_not_shifted_by_a_broker_tick(self):
        """If the bridge ever stamps bars in UTC and the tick stays broker
        time, the tick alone says +3h and would move a correct clock three
        hours wrong. The M15 bars contradict it, so nothing is shifted."""
        utc_h1 = [dict(c, ts=c["ts"] - OFF) for c in _broker_h1()]
        m15 = [{"ts": int(NOW - 300), "open": 1, "high": 1, "low": 1, "close": 1}]
        assert cycle_setup.utc_h1(utc_h1, _tick(), NOW, m15) == utc_h1

    def test_m15_bars_that_agree_change_nothing(self):
        m15 = [{"ts": int(NOW - 300 + OFF), "open": 1, "high": 1, "low": 1, "close": 1}]
        h1 = cycle_setup.utc_h1(_broker_h1(), _tick(), NOW, m15)
        assert ld.get_asia_range(h1) == (4040.0, 4050.0)

    def test_no_tick_leaves_the_bars_as_they_came(self):
        assert cycle_setup.utc_h1(_broker_h1(), None, NOW) == _broker_h1()


class _Bridge:
    def __init__(self, tick):
        self.tick = tick

    async def get_candles(self, tf, n):
        return {"H1": _broker_h1(), "M15": [], "H4": []}[tf]

    async def get_tick(self):
        return self.tick


class _Engine(svc.ReversalEngine):
    def __init__(self, bridge):
        self._bridge = bridge
        self._cached = {}
        self._status_msg = ""
        self._last_cycle_ts = None


@pytest.fixture
def seen(monkeypatch):
    seen = {"candidate": [], "all": []}

    async def _direct(fn, *a, **kw):
        return fn(*a, **kw)

    def _candidates(h1, price, **kw):
        seen["candidate"].append(ld.get_asia_range(h1))
        return []

    def _all(h1, price, extra=None):
        seen["all"].append(ld.get_asia_range(h1))
        return []

    monkeypatch.setattr("backend.src.services.cluster.node_roles.engines_generate_here",
                        lambda: True)
    monkeypatch.setattr("backend.src.db.database.to_db_thread", _direct)
    monkeypatch.setattr("backend.src.db.database.is_session_allowed", lambda: (True, "london"))
    monkeypatch.setattr("backend.src.db.database.get_risk_settings", lambda: {})
    monkeypatch.setattr(svc.re_db, "get_open_signals", lambda: [])
    monkeypatch.setattr(svc.re_db, "log_analysis", lambda entry: None)
    monkeypatch.setattr(svc.ld, "get_candidate_levels", _candidates)
    monkeypatch.setattr(svc.ld, "get_all_levels", _all)
    monkeypatch.setattr(svc.time, "time", lambda: NOW)
    return seen


class TestTheCycleUsesIt:
    def test_both_level_reads_see_the_utc_session(self, seen):
        asyncio.run(_Engine(_Bridge(_tick()))._run_cycle())
        assert seen["candidate"] == [(4040.0, 4050.0)]
        assert seen["all"] == [(4040.0, 4050.0)]

    def test_a_stale_tick_leaves_the_old_clock(self, seen):
        asyncio.run(_Engine(_Bridge(_tick(age_s=40 * 60)))._run_cycle())
        assert seen["candidate"] == [(3900.0, 4100.0)]
