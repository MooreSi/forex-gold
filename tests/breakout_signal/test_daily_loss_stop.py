"""The Breakout engine's daily loss stop exists (bugs/048, option 2).

`daily_loss_stop_usd` promised "Stop generating new signals for the rest of
the UTC day once today's closed PnL reaches this loss (Jul 2 ran to -$457
unchecked)" and nothing read it. Now `daily_loss_stop.suppress` is asked at
the top of `_process_candidate`, the one route to `create_signal` (the M5
cycle and the velocity monitor both go through it). Default $200, as the
catalogue always said.

The engine's own ledger is the measure: closed `bo_signals` whose close time
is today (UTC), net P&L where booked. No broker is involved.
"""
import os
import tempfile
import time
from datetime import datetime, timezone

import pytest

from backend.src.services.breakout_signal import breakout_signal_repo as repo
from backend.src.services.breakout_signal import daily_loss_stop as dls
from tests.conftest import remove_db_file


@pytest.fixture
def bo_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


def _closed(pnl, close_time=None):
    sid = repo.create_signal({"direction": "BUY", "breakout_type": "go",
                              "entry_mid": 2400.0, "stop_loss": 2390.0,
                              "signal_ref": f"BO-{pnl}-{close_time}"})
    repo.close_signal(sid, 2390.0, "loss" if pnl < 0 else "win", net_pnl_dollars=pnl)
    if close_time is not None:
        repo.get_db().run("UPDATE bo_signals SET close_time=? WHERE id=?", close_time, sid)


def test_a_day_past_the_limit_stops_generation(bo_db):
    _closed(-120.0)
    _closed(-90.0)
    entry = {}
    assert dls.suppress(entry, velocity=False) is True
    assert entry["result"] == "daily_loss_stop"
    assert "$-210.00" in entry["suppressed_reason"] or "-$210.00" in entry["suppressed_reason"]


def test_a_day_inside_the_limit_carries_on(bo_db):
    _closed(-150.0)
    entry = {}
    assert dls.suppress(entry, velocity=False) is False
    assert entry == {}


def test_wins_offset_losses(bo_db):
    _closed(-250.0)
    _closed(+100.0)
    assert dls.suppress({}, velocity=False) is False


def test_yesterday_does_not_count(bo_db):
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                  microsecond=0).timestamp()
    _closed(-400.0, close_time=midnight - 60)
    assert dls.suppress({}, velocity=False) is False


def test_the_limit_is_the_tunable(bo_db):
    from backend.src.services.breakout_signal import adaptive_params as ap
    repo.set_config("bo_ap:daily_loss_stop_usd", "100")
    assert ap.get("daily_loss_stop_usd") == 100.0
    _closed(-120.0)
    assert dls.suppress({}, velocity=False) is True


def test_a_velocity_candidate_is_stopped_without_writing_the_cycle_log(bo_db):
    _closed(-300.0)
    entry = {}
    assert dls.suppress(entry, velocity=True) is True
    assert entry == {}


def test_the_engine_asks_before_anything_else(bo_db, monkeypatch):
    """Wiring: with the stop in force, _process_candidate creates nothing and
    pays for no AI review. Both the M5 cycle and the velocity monitor reach
    create_signal only through it."""
    import asyncio
    from backend.src.services.breakout_signal import breakout_signal_service as svc

    created = []
    monkeypatch.setattr(svc.bdb, "create_signal", lambda d: created.append(d) or 1)
    asked = []
    monkeypatch.setattr(dls, "suppress", lambda entry, velocity: asked.append(velocity) or True)
    monkeypatch.setattr(svc.ap, "get", lambda k: (_ for _ in ()).throw(
        AssertionError(f"read {k} after the stop said no")))
    eng = svc.BreakoutEngine.__new__(svc.BreakoutEngine)
    asyncio.run(eng._process_candidate(
        {"direction": "BUY", "breakout_type": "go", "broken_level": 2400.0},
        {"price": 2401.0}, {}, 3.0, 25.0, velocity=True))
    assert asked == [True]
    assert created == []


def test_an_unreadable_ledger_fails_open(monkeypatch):
    def _boom(ts):
        raise RuntimeError("db gone")
    monkeypatch.setattr(dls.bdb, "closed_pnl_since", _boom)
    assert dls.suppress({}, velocity=False) is False
