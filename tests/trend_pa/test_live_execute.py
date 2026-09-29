"""Trend PA's one real-money path, driven with sentinels.

**No test here places, closes or modifies an MT5 order, real or demo.** The
main engine is a recorder. What is pinned is every reason the path REFUSES,
because each refusal is what stands between a virtual signal and the account:

  * no main engine linked
  * no strategy chosen for "Trend PA Engine" on Trading > Strategy -- the
    global default strategy is not assumed to honour a single 1:2 target
  * the Trading Schedule says no
  * an ARMED model expects a negative R

and what it sends when it does not refuse. Pause, daily-loss, daily-goal,
circuit breaker and max-open-trades are `open_trade`'s own gates, which every
source passes through; they are not re-implemented here.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.src.services.trend_pa import live_execute as lx
from backend.src.services.trend_pa import repo


class Sentinel:
    def __init__(self, fail=None):
        self.created, self.opened, self.fail = [], [], fail

    def create_signal(self, **kw):
        self.created.append(kw)
        return {"signal_id": "vsig-1"}

    async def open_trade_from_signal(self, signal_id, tick=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.opened.append(signal_id)
        return {"mt5_ticket": 555, "entry_price": 2000.2}


class Eng:
    def __init__(self, main=None, p=None):
        self._main_engine = main
        self.model = types.SimpleNamespace(predict=lambda f: p)


TICK = types.SimpleNamespace(bid=2000.0, ask=2000.2)


@pytest.fixture
def sig(tmp_path, monkeypatch):
    repo.init(str(tmp_path / "tpa.db"))
    s = {"created_at": 1.0, "origin": "live", "direction": "BUY", "pattern": "pin",
         "session": "london", "level": 1999.0, "level_kind": "swing_low",
         "entry": 2000.2, "stop_loss": 1998.0, "take_profit": 2004.6, "risk": 2.2,
         "atr_m15": 2.0, "spread": 0.2, "features": {}, "ml_prob": None}
    s["id"] = repo.insert_signal(s)
    s["signal_ref"] = f"TPA-{s['id']:04d}"
    monkeypatch.setattr(lx, "_strategy", lambda: "be_runner")
    monkeypatch.setattr(lx, "_schedule", lambda: (True, ""))
    yield s
    repo.close_db()


def _status():
    return repo.recent_signals(1)[0]["live_exec_status"]


def run(coro):
    return asyncio.run(coro)


def test_with_no_main_engine_nothing_is_sent(sig):
    run(lx.execute(Eng(main=None), sig, TICK))
    assert _status() == "skipped:no_main_engine"


def test_with_no_strategy_chosen_nothing_is_sent(sig, monkeypatch):
    monkeypatch.setattr(lx, "_strategy", lambda: None)
    main = Sentinel()
    run(lx.execute(Eng(main), sig, TICK))
    assert main.created == [] and _status() == "skipped:no_strategy"


def test_the_schedule_can_refuse(sig, monkeypatch):
    monkeypatch.setattr(lx, "_schedule", lambda: (False, "outside today's trading schedule"))
    main = Sentinel()
    run(lx.execute(Eng(main), sig, TICK))
    assert main.created == [] and _status().startswith("skipped:schedule")


def test_an_armed_model_expecting_a_loss_refuses(sig):
    main = Sentinel()
    run(lx.execute(Eng(main, p=0.25), sig, TICK))
    assert main.created == [] and _status().startswith("skipped:ML")


def test_an_unarmed_model_has_no_say(sig):
    main = Sentinel()
    run(lx.execute(Eng(main, p=None), sig, TICK))
    assert main.opened == ["vsig-1"]


def test_what_is_sent(sig):
    main = Sentinel()
    run(lx.execute(Eng(main, p=0.6), sig, TICK))
    [kw] = main.created
    assert kw["source_name"] == "Trend PA Engine"
    assert kw["direction"] == "BUY"
    assert kw["stop_loss"] == pytest.approx(1998.0)
    assert kw["tp1"] == pytest.approx(2004.6)
    assert kw["entry_low"] < 2000.2 < kw["entry_high"]
    assert kw["lot_size"] is None, "sizing is the account's risk settings, not this engine's"
    assert kw["notes"] == sig["signal_ref"]
    row = repo.recent_signals(1)[0]
    assert (row["live_exec_status"], row["mt5_ticket"], row["vantage_signal_id"]) == \
        ("success", 555, "vsig-1")


def test_a_refusal_from_the_order_path_is_recorded_not_raised(sig):
    run(lx.execute(Eng(Sentinel(fail="Trading paused until 23:59")), sig, TICK))
    assert _status().startswith("failed:Trading paused")


def test_the_real_schedule_gate_is_asked_under_the_engines_name(monkeypatch):
    seen = {}

    def fake(source="telegram", now=None):
        seen["source"] = source
        return True, ""
    import backend.src.services.risk.schedule as sched
    monkeypatch.setattr(sched, "check_trading_schedule", fake)
    assert lx._schedule() == (True, "")
    assert seen["source"] == "trend_pa_engine"
