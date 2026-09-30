"""Trend PA's clock and session (2026-10-01).

Three defects found by replaying the engine on the bridge's own history:

1. The replay reads `/candles_range`, which returns bars in TRUE UTC, and
   then subtracted the broker's +3h from them. Every session decision in the
   first measurement was made three hours late: "08:00-21:00" replayed as
   11:00-24:00 UTC. Measured correctly, the strategy as specified made
   -0.004R a trade over 640 trades, not +0.054R.
2. The session is now two Expert Tunables. Entries in 12:00-20:00 UTC made
   money in every year of the corrected replay; the London morning is what
   took it to zero. Defaults are the shipped 08-21, so nothing moves on
   upgrade.
3. The live cycle turned a broker bar stamp into UTC with a fixed +3h. The
   broker moves to +2h on 2026-10-25, after which each signal would block the
   next hour of bars as "already traded". The offset is now read off the
   forming bar, which is always less than one bar old.

**No test here places an order.** The bridge is a fake and the live path is
off (`tpa_live_execution` 0).
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone

import pytest

from backend.src.services.risk import expert_params as ep
from backend.src.services.trend_pa import backtest as bt
from backend.src.services.trend_pa import repo, service
from backend.src.services.trend_pa import strategy as st

M15, H1, H4 = 900, 3600, 14400
T0 = 1_790_000_100 // M15 * M15


def _series(step, n, start=T0, price=2000.0):
    return [{"ts": start + i * step, "open": price, "high": price + 1,
             "low": price - 1, "close": price} for i in range(n)]


# ── 1. the replay's clock ────────────────────────────────────────────────────

def test_the_replay_can_be_told_its_bars_are_already_utc(monkeypatch):
    seen = []

    def spy(h4, h1, m15, now_utc, params=None):
        seen.append(now_utc.timestamp() - (m15[-1]["ts"] + M15))
        return "nothing"
    monkeypatch.setattr(bt.st, "evaluate", spy)
    bt.run(_series(H4, 400, start=T0 - 400 * H4), _series(H1, 400, start=T0 - 400 * H1),
           _series(M15, 50), offset_s=0)
    assert set(seen) == {0}


class _RangeBridge:
    def __init__(self):
        self.asked = []

    async def get_candles_range(self, start, end, tf):
        self.asked.append(tf)
        step = {"H4": H4, "H1": H1, "M15": M15}[tf]
        first = int(start // step * step)
        return [{"ts": t, "open": 2000, "high": 2001, "low": 1999, "close": 2000}
                for t in range(first, int(min(end, T0 + 3 * 86400)), step)][:3000]


@pytest.fixture
def eng(tmp_path, monkeypatch):
    repo.init(str(tmp_path / "tpa.db"))
    e = service.TrendPAEngine(_RangeBridge(), model_path=tmp_path / "m.pkl")
    yield e
    repo.close_db()


def test_the_engines_replay_reads_its_range_bars_as_utc(eng, monkeypatch):
    got = {}

    def fake_run(h4, h1, m15, params=None, **kw):
        got.update(kw, params=params)
        return []
    monkeypatch.setattr(service.bt, "run", fake_run)
    monkeypatch.setattr(service.time, "time", lambda: T0 + 86400)
    asyncio.run(eng.run_backtest(days=2))
    assert got["offset_s"] == 0


# ── 2. the session tunables ──────────────────────────────────────────────────

def test_the_session_defaults_are_the_shipped_ones(fresh_db):
    p = service.session_params()
    assert p == {"session_start_utc": st.DEFAULTS["session_start_utc"],
                 "session_end_utc": st.DEFAULTS["session_end_utc"]}
    assert (p["session_start_utc"], p["session_end_utc"]) == (8, 21)


def test_the_session_follows_the_tunables(fresh_db):
    ep.set_params({"tpa_session_start_utc": 12, "tpa_session_end_utc": 20})
    assert service.session_params() == {"session_start_utc": 12, "session_end_utc": 20}


def test_the_live_cycle_and_the_replay_both_use_them(eng, fresh_db, monkeypatch):
    ep.set_params({"tpa_session_start_utc": 12, "tpa_session_end_utc": 20})
    seen = []

    def spy(h4, h1, m15, now_utc, params=None):
        seen.append(params)
        return "outside London/New York"
    monkeypatch.setattr(service.st, "evaluate", spy)

    class _Live:
        async def get_candles(self, tf, n):
            return _series({"H4": H4, "H1": H1, "M15": M15}[tf], n)
    eng._bridge = _Live()
    monkeypatch.setattr(service, "_generates_here", _true)
    asyncio.run(eng._run_cycle())
    assert seen[-1]["session_start_utc"] == 12 and seen[-1]["session_end_utc"] == 20

    got = {}
    monkeypatch.setattr(service.bt, "run", lambda *a, params=None, **k: got.update(params=params) or [])
    eng._bridge = _RangeBridge()
    monkeypatch.setattr(service.time, "time", lambda: T0 + 86400)
    asyncio.run(eng.run_backtest(days=2))
    assert got["params"]["session_start_utc"] == 12


def test_a_twelve_to_twenty_session_refuses_the_london_morning():
    p = {**st.DEFAULTS, "session_start_utc": 12, "session_end_utc": 20}
    assert st.session_of(datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc), p) is None
    assert st.session_of(datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc), p) is not None
    assert st.session_of(datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc), p) is None


async def _true():
    return True


# ── the replay is redone when what it measured changes ──────────────────────

def test_a_replay_from_an_older_build_or_session_is_stale(eng, fresh_db):
    assert eng.backtest_stale()                        # nothing replayed yet
    repo.replace_backtest([{"created_at": 1.0, "direction": "BUY", "pattern": "pin",
                            "entry": 1, "stop_loss": 0, "take_profit": 3, "risk": 1,
                            "level": 1, "level_kind": "swing_low", "session": "london",
                            "atr_m15": 1, "features": {}, "outcome": "win",
                            "exit_price": 3, "closed_at": 2.0, "r_net": 2.0}])
    assert eng.backtest_stale()                        # rows, but no version
    repo.set_config("backtest_version", service.backtest_version())
    assert not eng.backtest_stale()
    ep.set_params({"tpa_session_start_utc": 12})
    assert eng.backtest_stale()                        # the session moved


# ── 3. the live clock across the broker's DST change ─────────────────────────

class _WinterBridge:
    """Bars stamped the way the broker stamps them after 2026-10-25: UTC+2."""
    OFFSET = 7200

    def __init__(self, now):
        self.now = now
        self.tick = types.SimpleNamespace(bid=2000.0, ask=2000.2)

    def _bars(self, step, n):
        forming = int((self.now + self.OFFSET) // step * step)
        return _series(step, n, start=forming - (n - 1) * step)

    async def get_candles(self, tf, n):
        return self._bars({"H4": H4, "H1": H1, "M15": M15}[tf], n)

    async def get_tick(self):
        return self.tick


def _setup():
    return st.Setup(direction="BUY", pattern="pin", entry=2000.0, stop_loss=1998.0,
                    take_profit=2004.0, risk=2.0, level=1999.0, level_kind="swing_low",
                    session="london", atr_m15=2.0, atr_h4=8.0, features={})


def test_the_offset_is_read_off_the_forming_bar():
    now = 1_792_000_000.0
    for offset in (7200, 10800):
        forming = (now + offset) // M15 * M15
        assert service.broker_offset(forming, now) == offset


def test_after_the_broker_moves_to_winter_time_the_next_bar_can_trade(eng, monkeypatch):
    monkeypatch.setattr(service, "_generates_here", _true)
    monkeypatch.setattr(service, "_live_settings", lambda: {"tpa_live_execution": 0})
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    now = [1_792_000_000.0 // M15 * M15 + 30]
    monkeypatch.setattr(service.time, "time", lambda: now[0])
    eng._bridge = _WinterBridge(now[0])
    asyncio.run(eng._run_cycle())
    [sig] = repo.open_signals()
    # the bar it traded closed at `now - 30`, in true UTC
    assert sig["created_at"] == pytest.approx(now[0])
    repo.close_signal(sig["id"], "loss", 1998.0, now[0] + 60, -1.0)

    now[0] += M15                                     # the next bar has closed
    eng._bridge = _WinterBridge(now[0])
    asyncio.run(eng._run_cycle())
    assert len(repo.open_signals()) == 1, "a fixed +3h would block this bar for an hour"
