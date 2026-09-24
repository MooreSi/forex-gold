"""A backtest must not stop the app managing trades while it computes.

Measured on the owner's Mac, 2026-09-20 and 09-22: `POST /api/backtest/run`
held the event loop for **85.6 s, 85.97 s, 35.3 s, 34.9 s and 34.0 s**. The
walk is pure Python over tens of thousands of bars, and the route called it
directly inside an `async def`. Nothing else in the process runs while that
happens -- not the position monitor, not the EA link, not Telegram, not the
bridge watchdog. Two of those stalls began seconds after a batch of test
templates was saved, which is exactly when an owner runs one backtest after
another.

Each heavy step now runs on a worker thread. The check is whether the step
can see a running event loop: on the loop's own thread it can, on a worker it
cannot. That is the property that matters, and it is checkable without timing
anything.

The existing route tests stub these same controller names, and still do: the
route looks them up at call time and only changes WHERE it calls them.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.api.routers import backtest as bt_router


def _on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def where(monkeypatch, sentinel_engine):
    seen: dict = {}

    def _record(name, value):
        def _fn(*a, **k):
            seen[name] = _on_the_event_loop()
            return value
        return _fn

    async def _candles(symbol, timeframe, count):
        return [{"ts": 1.0}] * 10

    async def _ticks(from_ts, to_ts):
        return [{"ts": 1.0}] * 10

    sentinel_engine.get_candles_for_symbol = _candles
    sentinel_engine.get_ticks_range = _ticks
    monkeypatch.setattr(bt_router.bt_ctl, "signals_from_db",
                        _record("signals_from_db", ["s1"]))
    monkeypatch.setattr(bt_router.bt_ctl, "filter_signals",
                        _record("filter_signals", (["s1"], {})))
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest",
                        _record("run_backtest", {}))
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest_ticks",
                        _record("run_backtest_ticks", {}))
    return seen


def _run(client, **over):
    body = {"strategies": ["scale_out"], "timeframe": "M5", "days": 30}
    body.update(over)
    return client.post("/api/backtest/run", json=body)


@pytest.mark.parametrize("step", ["signals_from_db", "filter_signals", "run_backtest"])
def test_a_candle_backtest_runs_off_the_event_loop(make_client, where, step):
    assert _run(make_client()).status_code == 200

    assert where[step] is False, f"{step} ran on the event loop"


def test_a_tick_backtest_runs_off_the_event_loop(make_client, where):
    assert _run(make_client(), granularity="ticks").status_code == 200

    assert where["run_backtest_ticks"] is False
