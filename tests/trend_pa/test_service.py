"""The Trend PA engine's loop: what it does with a setup, a tick and a stop.

**No test here places, closes or modifies an MT5 order.** The bridge is a
fake that returns canned candles and ticks; the main engine is a sentinel
that records what it was asked to do. The strategy itself is replaced where
a test is about the loop rather than the setup (test_strategy.py covers it).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import types

import pytest

from backend.src.services.trend_pa import ml, repo, service
from backend.src.services.trend_pa import strategy as st


def _bars(n, start=1_000_000, step=900, price=2000.0):
    return [{"ts": start + i * step, "open": price, "high": price + 1,
             "low": price - 1, "close": price} for i in range(n)]


class FakeBridge:
    def __init__(self, bid=2000.0, ask=2000.2):
        self.tick = types.SimpleNamespace(bid=bid, ask=ask)
        self.asked = []
        self.m15 = _bars(70)

    async def get_tick(self):
        return self.tick

    async def get_fresh_tick(self):
        return self.tick

    async def get_candles(self, tf, count):
        self.asked.append((tf, count))
        return {"M15": self.m15, "H1": _bars(160, step=3600),
                "H4": _bars(210, step=14400)}[tf][-count:]


class Sentinel:
    """Stands in for the main engine. Records; never trades."""
    def __init__(self):
        self.created, self.opened = [], []

    def create_signal(self, **kw):
        self.created.append(kw)
        return {"signal_id": "vsig-1"}

    async def open_trade_from_signal(self, signal_id, tick=None):
        self.opened.append(signal_id)
        return {"mt5_ticket": 555, "entry_price": 2000.2}


@pytest.fixture
def eng(tmp_path, monkeypatch):
    repo.init(str(tmp_path / "tpa.db"))
    e = service.TrendPAEngine(FakeBridge(), model_path=tmp_path / "m.pkl")
    monkeypatch.setattr(service, "_generates_here", lambda: _true())
    monkeypatch.setattr(service, "_live_settings", lambda: {"tpa_live_execution": 0})
    yield e
    repo.close_db()


async def _true():
    return True


def _setup(direction="BUY"):
    buy = direction == "BUY"
    return st.Setup(direction=direction, pattern="pin", entry=2000.0,
                    stop_loss=1998.0 if buy else 2002.0,
                    take_profit=2004.0 if buy else 1996.0, risk=2.0, level=1999.0,
                    level_kind="swing_low", session="london", atr_m15=2.0,
                    atr_h4=8.0, features={"wick_frac": 0.7})


def run(coro):
    return asyncio.run(coro)


# ── generating ───────────────────────────────────────────────────────────────

def test_the_forming_bar_is_never_shown_to_the_strategy(eng, monkeypatch):
    seen = {}

    def spy(h4, h1, m15, now_utc, params=None):
        seen.update(h4=h4, h1=h1, m15=m15)
        return "no clear H4 trend"
    monkeypatch.setattr(service.st, "evaluate", spy)
    run(eng._run_cycle())
    assert seen["m15"][-1]["ts"] == eng._bridge.m15[-2]["ts"]
    assert len(seen["h4"]) == service.CANDLES["H4"] - 1


def test_a_setup_becomes_an_open_virtual_signal_filled_at_the_ask(eng, monkeypatch):
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    run(eng._run_cycle())
    [sig] = repo.open_signals()
    assert sig["direction"] == "BUY" and sig["entry"] == pytest.approx(2000.2)
    assert sig["spread"] == pytest.approx(0.2)
    # The 1:2 is kept from the price actually paid, not the candle close.
    risk = sig["entry"] - sig["stop_loss"]
    assert sig["take_profit"] - sig["entry"] == pytest.approx(2 * risk)


def test_a_sell_fills_at_the_bid(eng, monkeypatch):
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup("SELL"))
    run(eng._run_cycle())
    assert repo.open_signals()[0]["entry"] == pytest.approx(2000.0)


def test_one_signal_at_a_time(eng, monkeypatch):
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    run(eng._run_cycle())
    eng._bridge.m15 = _bars(71)       # a new bar closes
    run(eng._run_cycle())
    assert len(repo.open_signals()) == 1


def test_the_same_closed_bar_is_not_traded_twice(eng, monkeypatch):
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    run(eng._run_cycle())
    [sig] = repo.open_signals()
    repo.close_signal(sig["id"], "loss", 1998.0, 1.0, -1.1)
    run(eng._run_cycle())             # same M15 bar still the newest closed
    assert repo.open_signals() == []


def test_a_refusal_is_logged_once_per_closed_bar(eng, monkeypatch):
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: "no clear H4 trend")
    run(eng._run_cycle())
    run(eng._run_cycle())
    assert len(repo.analysis_log(10)) == 1
    eng._bridge.m15 = _bars(71)
    run(eng._run_cycle())
    assert len(repo.analysis_log(10)) == 2


def test_nothing_is_generated_where_generation_does_not_run(eng, monkeypatch):
    """Same gate as Breakout and Reversal: the VPS, or a node centralized out."""
    async def _false():
        return False
    monkeypatch.setattr(service, "_generates_here", _false)
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    run(eng._run_cycle())
    assert repo.open_signals() == [] and eng._bridge.asked == []


def test_an_armed_model_scores_the_signal(eng, monkeypatch):
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    monkeypatch.setattr(eng.model, "predict", lambda f: 0.61)
    run(eng._run_cycle())
    assert repo.open_signals()[0]["ml_prob"] == pytest.approx(0.61)


# ── outcomes ─────────────────────────────────────────────────────────────────

def _open_one(eng, monkeypatch):
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    run(eng._run_cycle())
    return repo.open_signals()[0]


def test_the_target_on_the_bid_closes_a_buy_as_a_win(eng, monkeypatch):
    sig = _open_one(eng, monkeypatch)
    eng._bridge.tick = types.SimpleNamespace(bid=sig["take_profit"] + 0.1, ask=0)
    run(eng._check_outcomes(now=sig["created_at"] + 60))
    [row] = repo.closed_signals()
    assert row["outcome"] == "win" and row["r_net"] == pytest.approx(2.0)


def test_the_stop_closes_a_buy_as_a_loss(eng, monkeypatch):
    sig = _open_one(eng, monkeypatch)
    eng._bridge.tick = types.SimpleNamespace(bid=sig["stop_loss"] - 0.1, ask=0)
    run(eng._check_outcomes(now=sig["created_at"] + 60))
    assert repo.closed_signals()[0]["r_net"] == pytest.approx(-1.0)


def test_a_signal_past_its_maximum_hold_is_closed_at_the_market(eng, monkeypatch):
    sig = _open_one(eng, monkeypatch)
    eng._bridge.tick = types.SimpleNamespace(bid=2001.2, ask=2001.4)
    run(eng._check_outcomes(now=sig["created_at"] + service.MAX_HOLD_S + 1))
    [row] = repo.closed_signals()
    assert row["outcome"] == "timeout" and row["exit_price"] == pytest.approx(2001.2)


def test_a_close_refits_the_model(eng, monkeypatch):
    sig = _open_one(eng, monkeypatch)
    fits = []
    monkeypatch.setattr(eng, "refit", lambda: fits.append(1))
    eng._bridge.tick = types.SimpleNamespace(bid=sig["take_profit"], ask=0)
    run(eng._check_outcomes(now=sig["created_at"] + 60))
    assert fits == [1]


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_a_user_stop_is_remembered_and_a_stand_down_is_not(eng):
    eng.stop()
    assert repo.get_config("user_stopped") == "1"
    repo.set_config("user_stopped", "0")
    eng.stop(persist=False)
    assert repo.get_config("user_stopped") == "0"


def test_refit_trains_on_live_and_backtest_and_saves(eng):
    rows = []
    for i in range(80):
        rows.append({"created_at": i, "direction": "BUY", "entry": 1.0, "stop_loss": 0.5,
                     "take_profit": 2.0, "risk": 0.5, "features": {"wick_frac": (i % 2) - 0.5},
                     "outcome": "win" if i % 2 else "loss", "exit_price": 1.0,
                     "closed_at": 10 + i, "r_net": 1.0})
    repo.replace_backtest(rows)
    state = eng.refit()
    assert state["n"] == 80
    assert ml.Model.load(eng._model_path).state["n"] == 80


def test_live_off_sends_nothing_live_on_hands_over(eng, monkeypatch):
    from backend.src.services.trend_pa import live_execute
    calls = []

    async def rec(engine, sig, tick):
        calls.append(sig["id"])
    monkeypatch.setattr(live_execute, "execute", rec)
    monkeypatch.setattr(service.st, "evaluate", lambda *a, **k: _setup())
    run(eng._run_cycle())
    assert calls == []

    [sig] = repo.open_signals()
    repo.close_signal(sig["id"], "loss", 1998.0, 1.0, -1.0)
    monkeypatch.setattr(service, "_live_settings", lambda: {"tpa_live_execution": 1})
    # The fake bars are stamped in 1970, so the same-bar guard would see the
    # first signal as newer than any of them.
    monkeypatch.setattr(service.repo, "last_signal_time", lambda *a, **k: None)
    eng._bridge.m15 = _bars(71)
    run(eng._run_cycle())
    assert len(calls) == 1
