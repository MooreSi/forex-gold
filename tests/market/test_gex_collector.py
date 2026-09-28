"""The daily GLD option-chain snapshot (docs/todo/009).

Once a weekday from 22:00 London (after the US close), one snapshot: the raw
per-strike chain plus GEX, flip level and walls, and gold's spot from the
engine's bridge so the levels convert to XAUUSD. It collects data and
nothing else -- no signal, gate or order reads it yet.

Stored in reversal_engine.db, which is not per-environment, so the history
does not split when the account switches (the same reason the decision log
lives there).

Nothing here reaches the network, a broker or an order: the chain fetcher
and the bridge are fakes, and the database is private and temporary.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.src.db import database as db_module
from backend.src.services.market import gex_collector as gc
from backend.src.services.market import gex_repo
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from tests.conftest import remove_db_file

LONDON = ZoneInfo("Europe/London")
TUESDAY_2230 = datetime(2026, 9, 29, 22, 30, tzinfo=LONDON)


@pytest.fixture
def dbs(fresh_db):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_db.init(path)
    gc._last_attempt = 0.0
    yield
    re_db.close_db()
    remove_db_file(path)


def _chain():
    rows = [
        {"expiry": "2026-10-16", "strike": 380.0, "t_years": 17 / 365, "call_oi": 0, "put_oi": 4000,
         "call_iv": 0.18, "put_iv": 0.2},
        {"expiry": "2026-10-16", "strike": 400.0, "t_years": 17 / 365, "call_oi": 1500, "put_oi": 1500,
         "call_iv": 0.18, "put_iv": 0.18},
        {"expiry": "2026-10-16", "strike": 420.0, "t_years": 17 / 365, "call_oi": 5000, "put_oi": 0,
         "call_iv": 0.17, "put_iv": 0.2},
    ]
    return {"spot": 400.0, "rows": rows, "expiries": ["2026-10-16"]}


class _Bridge:
    def __init__(self, mid=4320.0):
        self.mid = mid

    async def get_fresh_tick(self):
        class T: pass
        t = T()
        t.mid = self.mid
        return t


def _run(**kw):
    kw.setdefault("now", TUESDAY_2230)
    kw.setdefault("fetcher", _chain)
    kw.setdefault("bridge", _Bridge())
    return asyncio.run(gc.gex_snapshot_sweep(None, **kw))


def test_a_snapshot_is_stored_with_its_levels_and_the_raw_chain(dbs):
    _run()
    snap = gex_repo.latest()
    assert snap["asof_date"] == "2026-09-29"
    assert snap["underlying"] == "GLD"
    assert snap["spot"] == 400.0
    assert snap["xau_spot"] == 4320.0
    assert snap["ratio"] == pytest.approx(10.8)
    assert snap["call_wall"] == 420.0
    assert snap["put_wall"] == 380.0
    assert snap["xau_call_wall"] == pytest.approx(4536.0)
    assert len(gex_repo.strikes(snap["id"])) == 3


def test_once_per_day(dbs):
    calls = []

    def _fetch():
        calls.append(1)
        return _chain()

    _run(fetcher=_fetch)
    _run(fetcher=_fetch)
    assert len(calls) == 1


def test_not_before_22_london_and_not_at_weekends(dbs):
    calls = []

    def _fetch():
        calls.append(1)
        return _chain()

    _run(fetcher=_fetch, now=datetime(2026, 9, 29, 21, 59, tzinfo=LONDON))
    _run(fetcher=_fetch, now=datetime(2026, 10, 3, 22, 30, tzinfo=LONDON))   # Saturday
    assert calls == []


def test_no_bridge_means_no_snapshot_and_the_day_stays_open(dbs):
    """No bridge: a remote node, or an engine not running. Without gold's
    price the levels cannot be put in XAUUSD, and a snapshot that cannot be
    joined to the engines is not worth storing."""
    _run(bridge=None)
    assert gex_repo.latest() is None
    _run()
    assert gex_repo.latest() is not None


def test_an_empty_or_failed_fetch_stores_nothing_and_retries_later(dbs):
    def _boom():
        raise RuntimeError("yahoo down")

    _run(fetcher=_boom)
    assert gex_repo.latest() is None
    assert db_module.get_app_config(gc.LAST_RUN_KEY) in (None, "")
    gc._last_attempt = 0.0   # past the retry wait, so the empty reply is really reached
    _run(fetcher=lambda: {"spot": 400.0, "rows": [], "expiries": []})
    assert gex_repo.latest() is None
    assert db_module.get_app_config(gc.LAST_RUN_KEY) in (None, "")


def test_a_failure_is_not_retried_every_minute(dbs):
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("yahoo down")

    _run(fetcher=_boom)
    _run(fetcher=_boom, now=datetime(2026, 9, 29, 22, 31, tzinfo=LONDON))
    assert len(calls) == 1


def test_it_records_the_sign_convention_it_assumed(dbs):
    _run()
    assert "long calls" in gex_repo.latest()["assumptions"]


def test_a_bridge_with_no_usable_price_takes_no_snapshot(dbs):
    _run(bridge=_Bridge(mid=0.0))
    assert gex_repo.latest() is None


def test_the_same_day_is_never_stored_twice(dbs):
    _run()
    snap, strikes = gc._snapshot(_chain(), 4320.0, "2026-09-29")
    assert gex_repo.insert_snapshot(snap, strikes) is None


def test_the_bridge_is_the_running_reversal_engines(monkeypatch):
    from backend.src.services.reversal_engine import reversal_engine_service as svc

    class _Engine:
        _bridge = "the bridge"

    monkeypatch.setattr(svc, "get_instance", lambda: _Engine())
    assert gc._engine_bridge() == "the bridge"
    monkeypatch.setattr(svc, "get_instance", lambda: None)
    assert gc._engine_bridge() is None


# ── The Yahoo fetch, against a fake yfinance ────────────────────────────────

class _Frame:
    """Just enough of a DataFrame: iterrows() and an iloc'd Close."""
    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        return enumerate(self._rows)


class _Hist:
    def __getitem__(self, key):
        class _Col:
            class iloc:
                def __class_getitem__(cls, i):
                    return 393.41
        return _Col


class _Chain:
    def __init__(self, calls, puts):
        self.calls, self.puts = _Frame(calls), _Frame(puts)


class _Ticker:
    options = ["2026-09-25", "2026-10-02", "2026-10-16", "2027-03-19"]

    def __init__(self, symbol):
        assert symbol == "GLD"

    def history(self, period):
        return _Hist()

    def option_chain(self, exp):
        nan = float("nan")
        return _Chain(
            calls=[{"strike": 400.0, "openInterest": 1200.0, "impliedVolatility": 0.18},
                   {"strike": 410.0, "openInterest": nan, "impliedVolatility": nan}],
            puts=[{"strike": 400.0, "openInterest": 900.0, "impliedVolatility": 0.2}],
        )


@pytest.fixture
def fake_yf(monkeypatch):
    import sys
    import types
    mod = types.ModuleType("yfinance")
    mod.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", mod)


def test_fetch_keeps_only_expiries_within_the_window_and_merges_calls_and_puts(fake_yf):
    from datetime import timezone
    now = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)
    got = gc.fetch_chain(now=now)
    assert got["spot"] == 393.41
    assert got["expiries"] == ["2026-10-02", "2026-10-16"]   # past and >60 days dropped
    k400 = [r for r in got["rows"] if r["strike"] == 400.0 and r["expiry"] == "2026-10-02"][0]
    assert (k400["call_oi"], k400["put_oi"]) == (1200.0, 900.0)
    assert (k400["call_iv"], k400["put_iv"]) == (0.18, 0.2)
    assert k400["t_years"] > 0


def test_fetch_reads_missing_open_interest_as_zero_and_missing_iv_as_none(fake_yf):
    from datetime import timezone
    got = gc.fetch_chain(now=datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc))
    k410 = [r for r in got["rows"] if r["strike"] == 410.0][0]
    assert k410["call_oi"] == 0.0
    assert k410["call_iv"] is None


def test_fetch_stops_at_the_expiry_cap(fake_yf, monkeypatch):
    from datetime import timezone
    monkeypatch.setattr(gc, "MAX_EXPIRIES", 1)
    got = gc.fetch_chain(now=datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc))
    assert got["expiries"] == ["2026-10-02"]
