"""The proven-edge gate runs on the live path, and refuses when unsure.

docs/todo/reversal-engine/240. Drives `_try_live_execute` with `_main_eng`
None, so anything that got past every gate returns without an order rather
than reaching a broker. Nothing here places, closes or modifies an order.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.reversal_engine import edge_model
from backend.src.services.reversal_engine.ml_engine import FEATURE_NAMES
from backend.src.services.reversal_engine.reversal_engine_live_execute import (
    _LiveExecuteMixin)


def _candles(n, px=3310.0):
    # Gently alternating, so no trend, no exhaustion, no bias.
    return [{"ts": 1_790_000_000 + 60 * i, "open": px, "high": px + 0.8,
             "low": px - 0.8, "close": px + (0.2 if i % 2 else -0.2)}
            for i in range(n)]


class _Bridge:
    def __init__(self, candles=None):
        self._candles = candles

    async def get_candles(self, timeframe, count):
        return list(self._candles or [])[-count:]


class _Engine(_LiveExecuteMixin):
    # The real engine's indicator helpers, so the fill-time re-score runs.
    from backend.src.services.reversal_engine.reversal_engine_service import (
        ReversalEngine as _RE)
    _calc_atr = staticmethod(_RE._calc_atr)
    _calc_adx = staticmethod(_RE._calc_adx)

    def __init__(self, bridge=None):
        self._main_eng = None
        self._bridge = bridge or _Bridge()

    async def _ref_cadence_stats(self):
        return (240.0, 0)


@pytest.fixture
def statuses(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute"
        ".re_db.update_live_exec",
        lambda sig_id, status=None, **kw: seen.__setitem__(sig_id, status))
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute"
        ".re_db.store_ml_prob_at_fill", lambda *a, **k: None)
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute"
        ".re_db.get_recent_win_rate", lambda n: 0.5)
    return seen


@pytest.fixture
def clear_path(monkeypatch):
    monkeypatch.setattr("backend.src.services.risk.schedule.check_trading_schedule",
                        lambda **kw: (True, ""))
    monkeypatch.setattr("backend.src.utils.news_calendar.check_news_blackout",
                        lambda: (True, ""))
    # The production model has no opinion, so its gate cannot be the one
    # that stops the flow.
    monkeypatch.setattr("backend.src.services.reversal_engine.ml_engine.predict",
                        lambda f: None)


@pytest.fixture
def decisions(monkeypatch):
    calls = []

    def spy(answer):
        def _decide(features):
            calls.append(features)
            return answer
        monkeypatch.setattr(edge_model, "decide", _decide)
    spy.calls = calls
    return spy


def _settings(monkeypatch, **extra):
    rs = {"re_live_execution": 1, "strategy_lot_size": 0.01}
    rs.update(extra)
    monkeypatch.setattr("backend.src.db.database.get_risk_settings", lambda: rs)


def _run(engine):
    sig = {"id": 1, "signal_ref": "RE-1", "direction": "BUY", "level_price": 3300.0,
           "atr": 8.0, "level_type": "round_10", "level_score": 0.5,
           "entry_low": 3299.0, "entry_high": 3301.0, "created_at": 1_790_000_000.0}
    asyncio.run(engine._try_live_execute(sig, 3300.0, None))


class TestOffChangesNothing:
    def test_off_the_edge_model_is_not_even_asked(self, monkeypatch, statuses,
                                                  clear_path, decisions):
        decisions((False, "never", None))
        _settings(monkeypatch)
        _run(_Engine(_Bridge(_candles(80))))
        assert decisions.calls == []
        assert statuses.get(1) != "skipped:unproven_edge"


class TestOnItRefusesUnlessProven:
    def test_the_real_unfitted_model_refuses(self, monkeypatch, statuses, clear_path):
        monkeypatch.setattr(edge_model, "_instance", edge_model.EdgeModel())
        _settings(monkeypatch, re_require_proven_edge=1)
        _run(_Engine(_Bridge(_candles(80))))
        assert statuses.get(1) == "skipped:unproven_edge"

    def test_a_refusal_stops_the_fill(self, monkeypatch, statuses, clear_path,
                                      decisions):
        decisions((False, "edge model not proven: t=-4.01", None))
        _settings(monkeypatch, re_require_proven_edge=1)
        _run(_Engine(_Bridge(_candles(80))))
        assert statuses.get(1) == "skipped:unproven_edge"

    def test_a_pass_lets_the_signal_through_the_gate(self, monkeypatch, statuses,
                                                     clear_path, decisions):
        decisions((True, "", 0.2))
        _settings(monkeypatch, re_require_proven_edge=1)
        _run(_Engine(_Bridge(_candles(80))))
        assert statuses.get(1) != "skipped:unproven_edge"

    def test_it_scores_the_fill_time_feature_vector(self, monkeypatch, statuses,
                                                    clear_path, decisions):
        decisions((True, "", 0.2))
        _settings(monkeypatch, re_require_proven_edge=1)
        _run(_Engine(_Bridge(_candles(80))))
        assert len(decisions.calls) == 1
        assert decisions.calls[0] is not None
        assert len(decisions.calls[0]) == len(FEATURE_NAMES)

    def test_when_the_fill_time_rescore_could_not_run_it_refuses(
            self, monkeypatch, statuses, clear_path):
        """No candles, so no fresh feature vector. Every other gate here
        passes on a failed re-evaluation; this one must not, or a bridge
        hiccup would be the way an unproven trade gets placed."""
        monkeypatch.setattr(edge_model, "_instance", edge_model.EdgeModel())
        _settings(monkeypatch, re_require_proven_edge=1)
        _run(_Engine(_Bridge([])))
        assert statuses.get(1) == "skipped:unproven_edge"
