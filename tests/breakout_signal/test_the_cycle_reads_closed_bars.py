"""The Breakout cycle judges CLOSED M5 candles, on a UTC clock (2026-10-17).

Every entry check says it reads "the last closed candle": `check_breakout_go`
("most recently closed candle"), `check_breakout_retest` ("the last CLOSED
candle confirms rejection") and `check_liquidity_sweep` ("CLOSES back
inside"). The cycle handed them `get_candles("M5", 80)` as it came, and the
bridge's last bar is the one still forming. So once a minute the engine asked
how a candle had closed of a candle that had not, and its "close" was the live
price: a sweep "closed back inside" whenever price was momentarily back
inside. 140 of the engine's 157 closed signals were sweeps, and they won 34%
at a 1:1 first target. `backtest.py` replays closed bars, so it never measured
what the engine traded.

The H1 stamps were broker time too, so `market/levels._session_range` built
the "Asian" range from 20:00-04:59 UTC instead of 23:00-07:59.

No test here reaches a broker or a database.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.src.services.breakout_signal import breakout_signal_service as svc
from backend.src.services.breakout_signal import signal_generator as sg
from backend.src.services.market import levels
from backend.src.services.market.levels import identify_key_levels
from backend.src.utils.models import Tick

H = 3600
OFF = 3 * H                     # broker clock, US summer
# Wednesday 2026-10-14 12:02:30 UTC: the M5 bar that opened at 12:00 is forming.
NOW = datetime(2026, 10, 14, 12, 2, 30, tzinfo=timezone.utc).timestamp()
LAST_M5_OPEN = NOW - 150


def _bar(ts, close):
    return {"ts": int(ts), "open": close, "high": close + 1.0,
            "low": close - 1.0, "close": close, "volume": 10.0}


def _m5(n=81):
    """Broker-stamped M5 bars, oldest first; the last is forming and its
    close (999.0) is unmistakable."""
    bars = [_bar(LAST_M5_OPEN - 300 * k + OFF, 4000.0 + (n - k)) for k in range(n - 1, 0, -1)]
    return bars + [_bar(LAST_M5_OPEN + OFF, 999.0)]


def _h1(n=120):
    last_open = NOW - 150                           # 12:00 UTC, forming
    return [_bar(last_open - H * k + OFF, 4000.0 + k) for k in range(n - 1, -1, -1)]


def _tick(age_s=2.0):
    return Tick(bid=4010.0, ask=4010.3, mid=4010.15, spread=0.3, spread_points=30.0,
                timestamp=NOW + OFF - age_s, source="test")


class _Bridge:
    def __init__(self, tick):
        self.tick, self.asked = tick, []

    async def get_tick(self):
        return self.tick

    async def get_candles(self, tf, n):
        self.asked.append((tf, n))
        return {"M5": _m5(), "H1": _h1(), "H4": _h1(60)}[tf]


class _Repo:
    def __init__(self):
        self.analysis = []

    def log_analysis(self, entry):
        self.analysis.append(dict(entry))

    def get_open_signals(self):
        return []


class _Engine(svc.BreakoutEngine):
    def __init__(self, bridge):
        self._bridge = bridge
        self._cached = {}
        self.status_detail = ""
        self.last_cycle_at = None


@pytest.fixture
def seen(monkeypatch):
    seen = {"m5": [], "h1": [], "repo": _Repo()}

    async def _direct(fn, *a, **kw):
        return fn(*a, **kw)

    def _check(m5, *a, **kw):
        seen["m5"].append(m5)
        return None

    def _levels(h1, price, m15_candles=None):
        seen["h1"].append(h1)
        return []

    monkeypatch.setattr("backend.src.services.cluster.node_roles.engines_generate_here",
                        lambda: True)
    monkeypatch.setattr("backend.src.db.database.to_db_thread", _direct)
    monkeypatch.setattr("backend.src.db.database.is_session_allowed", lambda: (True, "london"))
    monkeypatch.setattr("backend.src.db.database.get_risk_settings", lambda: {})
    monkeypatch.setattr(svc, "bdb", seen["repo"])
    monkeypatch.setattr(svc, "get_session", lambda: "london")
    monkeypatch.setattr(svc, "is_news_window", lambda: False)
    monkeypatch.setattr(svc, "compute_htf_bias", lambda h1: "bullish")
    monkeypatch.setattr(svc, "compute_h4_bias", lambda h4: "bullish")
    monkeypatch.setattr(svc, "compute_adx", lambda c: 35.0)
    monkeypatch.setattr(svc, "compute_macd_hist", lambda closes: (0.0, 0.5))
    monkeypatch.setattr(svc.ap, "get", lambda key: {"min_adx_go": 30.0, "min_adx_retest": 24.0,
                                                    "max_spread_pts": 0.5}.get(key, 1.0))
    monkeypatch.setattr(svc, "identify_key_levels", _levels)
    for name in ("check_breakout_go", "check_breakout_retest", "check_liquidity_sweep"):
        monkeypatch.setattr(svc, name, _check)
    monkeypatch.setattr(svc.time, "time", lambda: NOW)
    return seen


def _cycle(tick):
    engine = _Engine(_Bridge(tick))
    asyncio.run(engine._run_cycle())
    return engine


class TestTheEntryChecksSeeClosedCandles:
    def test_all_three_checks_ran(self, seen):
        """Negative control: a recorder that is never called proves nothing."""
        _cycle(_tick())
        assert len(seen["m5"]) == 3

    def test_none_of_them_sees_the_forming_bar(self, seen):
        _cycle(_tick())
        assert all(m5[-1]["close"] != 999.0 for m5 in seen["m5"])

    def test_the_last_candle_they_see_is_the_last_one_that_closed(self, seen):
        _cycle(_tick())
        last = seen["m5"][0][-1]
        assert last["ts"] + 300 <= NOW < last["ts"] + 600

    def test_they_still_get_eighty_bars(self, seen):
        """The forming bar is replaced, not just removed: the cycle asks for
        one more, so the windows the checks were tuned on stay the same size."""
        _cycle(_tick())
        assert len(seen["m5"][0]) == 80


class TestTheClockIsUtc:
    def test_the_m5_stamps_are_utc(self, seen):
        _cycle(_tick())
        last = datetime.fromtimestamp(seen["m5"][0][-1]["ts"], timezone.utc)
        assert last.strftime("%H:%M") == "11:55"

    def test_the_h1_bars_handed_to_the_levels_are_utc_and_keep_the_forming_one(self, seen):
        """The forming H1 bar is context -- today's range so far -- and stays."""
        _cycle(_tick())
        h1 = seen["h1"][0]
        assert len(h1) == 120
        assert datetime.fromtimestamp(h1[-1]["ts"], timezone.utc).strftime("%H:%M") == "12:00"

    def test_a_tick_too_stale_to_set_the_clock_still_drops_the_forming_bar(self, seen):
        """A shut market: the cycle fetches before it asks about the weekend,
        so refusing here would replace "Market closed" with "No market data"
        all weekend. The stamps stay as they came (the old clock) and the
        forming bar still goes."""
        _cycle(_tick(age_s=40 * 60))
        assert seen["m5"] and seen["m5"][0][-1]["close"] != 999.0
        assert seen["h1"][0][-1]["ts"] == _h1()[-1]["ts"]


class TestATickWithoutItsBrokerStamp:
    def test_does_not_throw_the_recent_bars_away(self):
        """The client fills a missing tick stamp with its own UTC clock. Read
        alone that is "no offset", and against bars three hours ahead every
        bar of the last three hours would look unclosed. The bars veto it."""
        utc_tick = Tick(bid=4010.0, ask=4010.3, mid=4010.15, spread=0.3,
                        spread_points=30.0, timestamp=NOW, source="test")
        m5, _ = sg.cycle_candles(_m5(), _h1(), utc_tick, NOW)
        assert len(m5) == 80 and m5[-1]["close"] != 999.0


class TestTheAsianRangeUsesTheRightHours:
    """End to end through the real level finder. The 21:00 UTC bar is the
    day's extreme; on broker stamps it reads 00:00 and counted as Asian."""

    def _broker_h1(self):
        # 24 H1 bars ending with the one that opened 11:00 UTC on 2026-10-14.
        end = datetime(2026, 10, 14, 11, tzinfo=timezone.utc).timestamp()
        bars = []
        for k in range(23, -1, -1):
            t = end - H * k
            hour = datetime.fromtimestamp(t, timezone.utc).hour
            c = 4100.0 if hour == 21 else 4000.0
            bars.append(_bar(t + OFF, c))
        return bars

    def _asian(self, h1):
        # The session range itself: identify_key_levels merges levels within
        # 2.5 points, and the 21:00 bar is also a swing high, so the merged
        # list would name it "resistance" and hide which source produced it.
        return {x["type"]: x["price"] for x in levels._session_range(h1)}

    def test_the_level_finder_still_reads_the_session_range(self, monkeypatch):
        """Negative control for the shortcut above: what `_session_range`
        returns is what the engine's level list carries."""
        monkeypatch.setattr(levels, "_session_range",
                            lambda h1: [{"price": 4077.0, "type": "asian_high", "strength": 2}])
        _, h1 = sg.cycle_candles(_m5(), self._broker_h1(), _tick(), NOW)
        got = {x["type"]: x["price"] for x in identify_key_levels(h1, 4050.0)}
        assert got.get("asian_high") == 4077.0

    def test_on_broker_stamps_the_new_york_close_counted_as_asian(self):
        """The defect, reproduced: the 21:00 UTC bar's 4101 high is taken as
        the Asian high."""
        assert self._asian(self._broker_h1()).get("asian_high") == pytest.approx(4101.0)

    def test_through_cycle_candles_it_does_not(self):
        _, h1 = sg.cycle_candles(_m5(), self._broker_h1(), _tick(), NOW)
        assert self._asian(h1).get("asian_high") == pytest.approx(4001.0)
