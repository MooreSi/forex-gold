"""The new capability gates actually run on the live path.

docs/todo/reversal-engine/200. A gate that exists, is tested in isolation and
is never consulted is the failure this repo was built to prevent -- a
guardrail script scanned a deleted directory and printed "all good" for
months.

Each test drives `_try_live_execute` and asserts it returns before reaching
the order path. `_main_eng` is None throughout, so anything that got past a
gate would raise rather than quietly pass.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest import mock

import pytest

from backend.src.services.reversal_engine.reversal_engine_live_execute import (
    _LiveExecuteMixin)


class _Bridge:
    def __init__(self, candles=None):
        self._candles = candles or []

    async def get_candles(self, timeframe, count):
        return list(self._candles)


class _Engine(_LiveExecuteMixin):
    def __init__(self, bridge=None):
        self._main_eng = None
        self._bridge = bridge or _Bridge()


@pytest.fixture
def statuses(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute"
        ".re_db.update_live_exec",
        lambda sig_id, status=None, **kw: seen.__setitem__(sig_id, status))
    return seen


@pytest.fixture
def clear_path(monkeypatch):
    """Live execution on, schedule and news clear, so the gate under test is
    the only thing that can stop the flow."""
    monkeypatch.setattr("backend.src.services.risk.schedule.check_trading_schedule",
                        lambda **kw: (True, ""))
    monkeypatch.setattr("backend.src.utils.news_calendar.check_news_blackout",
                        lambda: (True, ""))


def _settings(monkeypatch, **extra):
    rs = {"re_live_execution": 1, "strategy_lot_size": 0.01}
    rs.update(extra)
    monkeypatch.setattr("backend.src.db.database.get_risk_settings", lambda: rs)
    return rs


def _run(engine, sig=None):
    sig = sig or {"id": 1, "signal_ref": "RE-1", "direction": "BUY",
                  "level_price": 3300.0, "atr": 8.0}
    return asyncio.run(engine._try_live_execute(sig, 3300.0, None))


class TestTheLiquidityGate:
    def test_off_by_default_it_does_not_block(self, monkeypatch, statuses, clear_path):
        _settings(monkeypatch)
        with mock.patch("backend.src.services.risk.capability_gates.liquidity_blocks",
                        return_value=None) as m:
            _run(_Engine())
        assert statuses.get(1) != "skipped:liquidity"
        assert m.called, "the gate must be consulted even when it is off"

    def test_a_blocked_window_stops_the_fill(self, monkeypatch, statuses, clear_path):
        _settings(monkeypatch, session_liquidity_gate_enabled=1)
        ts = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch("time.time", return_value=ts):
            _run(_Engine())
        assert statuses.get(1) == "skipped:liquidity"


class TestTheEntryTrigger:
    def _rejecting_candles(self):
        # Wicks below 3300 and closes back above it.
        return [{"ts": 1, "open": 3301, "high": 3302, "low": 3297, "close": 3301}]

    def _failing_candles(self):
        # Never traded through the level at all.
        return [{"ts": 1, "open": 3305, "high": 3306, "low": 3303, "close": 3305}]

    def test_off_by_default_no_candles_are_even_fetched(self, monkeypatch,
                                                        statuses, clear_path):
        _settings(monkeypatch)
        engine = _Engine(_Bridge(self._failing_candles()))
        _run(engine)
        assert statuses.get(1) != "skipped:no_trigger"

    def test_an_unconfirmed_level_is_not_traded(self, monkeypatch, statuses,
                                                clear_path):
        _settings(monkeypatch, entry_trigger_enabled=1, entry_trigger_rejection=1)
        _run(_Engine(_Bridge(self._failing_candles())))
        assert statuses.get(1) == "skipped:no_trigger"

    def test_a_confirmed_level_passes_the_gate(self, monkeypatch, statuses,
                                               clear_path):
        _settings(monkeypatch, entry_trigger_enabled=1, entry_trigger_rejection=1)
        _run(_Engine(_Bridge(self._rejecting_candles())))
        assert statuses.get(1) != "skipped:no_trigger"

    def test_a_bridge_failure_does_not_block_the_trade(self, monkeypatch,
                                                       statuses, clear_path):
        """A confirmation the app could not evaluate because its own data
        feed failed is not evidence against the setup. Blocking on it would
        turn a bridge hiccup into a silent trading halt, and the news gate
        next door already takes the same view."""
        class _Broken(_Bridge):
            async def get_candles(self, timeframe, count):
                raise RuntimeError("bridge down")

        _settings(monkeypatch, entry_trigger_enabled=1, entry_trigger_rejection=1)
        _run(_Engine(_Broken()))
        assert statuses.get(1) != "skipped:no_trigger"


class TestTheMetaLabelGate:
    def test_off_by_default_it_does_not_block(self, monkeypatch, statuses,
                                              clear_path):
        _settings(monkeypatch)
        _run(_Engine())
        assert statuses.get(1) != "meta_skipped"

    def test_on_but_unarmed_it_does_not_block(self, monkeypatch, statuses,
                                              clear_path):
        """The labeller refuses to arm until it has costed trades to learn
        from, and an unarmed model has no opinion. Turning the switch on
        before the data exists must not halt trading."""
        _settings(monkeypatch, meta_label_gate_enabled=1)
        _run(_Engine())
        assert statuses.get(1) != "meta_skipped"

    def test_an_armed_model_below_threshold_blocks(self, monkeypatch, statuses,
                                                   clear_path):
        _settings(monkeypatch, meta_label_gate_enabled=1, meta_label_threshold=0.6)
        with mock.patch("backend.src.services.reversal_engine.meta_label"
                        ".score_signal", return_value=0.2):
            _run(_Engine())
        assert statuses.get(1) == "meta_skipped"

    def test_an_armed_model_above_threshold_passes(self, monkeypatch, statuses,
                                                   clear_path):
        _settings(monkeypatch, meta_label_gate_enabled=1, meta_label_threshold=0.6)
        with mock.patch("backend.src.services.reversal_engine.meta_label"
                        ".score_signal", return_value=0.9):
            _run(_Engine())
        assert statuses.get(1) != "meta_skipped"
