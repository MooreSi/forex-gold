"""The fill-time ML re-score reads the same features the creation-time one did.

1. FVG context. `reversal_engine_service` measures it at creation from the M15
   candles and hands it to `extract_features` -- but it is not an `re_signals`
   column, so `dict(sig)` at fill time has none of it. All four FVG features
   fell back to "no gap", and `pro_fvg_delta` / `pro_likeness` were computed
   from that "no gap" too. The ML gate decided on that vector. It is now
   measured again at fill time, against the fresh candles, like every other
   dynamic input there.

2. No prediction is not a prediction of zero. `store_ml_prob_at_fill` was
   handed `fresh_prob or 0.0`, so "the model had no opinion" was recorded as
   "the model predicted breakeven".

Drives `_try_live_execute` with `_main_eng` None, so a signal that clears
every gate returns without an order. Nothing here places, closes or modifies
an order.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.reversal_engine import ml_engine as ml
from backend.src.services.reversal_engine.reversal_engine_live_execute import (
    _LiveExecuteMixin)

N = ml.FEATURE_NAMES
_GAP = {"fvg_confluence": 1.0, "fvg_dist_norm": 0.3, "fvg_fresh": 1.0,
        "fvg_size_norm": 0.7}


def _candles(n, px=3310.0):
    return [{"ts": 1_790_000_000 + 60 * i, "open": px, "high": px + 0.8,
             "low": px - 0.8, "close": px + (0.2 if i % 2 else -0.2)}
            for i in range(n)]


class _Bridge:
    async def get_candles(self, timeframe, count):
        return _candles(80)[-count:]


class _Engine(_LiveExecuteMixin):
    from backend.src.services.reversal_engine.reversal_engine_service import (
        ReversalEngine as _RE)
    _calc_atr = staticmethod(_RE._calc_atr)
    _calc_adx = staticmethod(_RE._calc_adx)

    def __init__(self):
        self._main_eng = None
        self._bridge = _Bridge()

    async def _ref_cadence_stats(self):
        return (240.0, 0)


@pytest.fixture
def stored(monkeypatch):
    calls = []
    p = "backend.src.services.reversal_engine.reversal_engine_live_execute.re_db."
    monkeypatch.setattr(p + "update_live_exec", lambda *a, **k: None)
    monkeypatch.setattr(p + "store_ml_prob_at_fill",
                        lambda sig_id, prob, htf: calls.append(prob))
    monkeypatch.setattr(p + "get_recent_win_rate", lambda n: 0.5)
    monkeypatch.setattr("backend.src.services.risk.schedule.check_trading_schedule",
                        lambda **kw: (True, ""))
    monkeypatch.setattr("backend.src.utils.news_calendar.check_news_blackout",
                        lambda: (True, ""))
    monkeypatch.setattr("backend.src.db.database.get_risk_settings",
                        lambda: {"re_live_execution": 1, "strategy_lot_size": 0.01})
    return calls


@pytest.fixture
def scored(monkeypatch):
    seen = []

    def _predict(features):
        seen.append(features)
        return None
    monkeypatch.setattr(ml, "predict", _predict)
    return seen


def _run(**over):
    sig = {"id": 1, "signal_ref": "RE-1", "direction": "BUY", "level_price": 3300.0,
           "atr": 8.0, "level_type": "round_10", "level_score": 0.5,
           "entry_low": 3299.0, "entry_high": 3301.0, "created_at": 1_790_000_000.0}
    sig.update(over)
    asyncio.run(_Engine()._try_live_execute(sig, 3300.0, None))


class TestTheFvgContextIsMeasuredAgain:
    def test_the_fill_time_vector_carries_the_gap(self, monkeypatch, stored, scored):
        asked = []

        def _fvg(candles, entry, direction, atr):
            asked.append((entry, direction))
            return dict(_GAP)
        monkeypatch.setattr(
            "backend.src.services.reversal_engine.ict_patterns.fvg_context", _fvg)

        _run()

        assert scored, "the fill-time re-score never ran"
        v = scored[0]
        for name, value in _GAP.items():
            assert v[N.index(name)] == value, name
        assert asked == [(3300.0, "BUY")], "measured against the wrong level"

    def test_a_failed_measurement_falls_back_to_no_gap(self, monkeypatch, stored, scored):
        """Control: the same neutrals creation uses when it cannot measure."""
        def _boom(*a, **k):
            raise RuntimeError("no candles")
        monkeypatch.setattr(
            "backend.src.services.reversal_engine.ict_patterns.fvg_context", _boom)

        _run()

        assert scored[0][N.index("fvg_dist_norm")] == 5.0
        assert scored[0][N.index("fvg_confluence")] == 0.0


class TestNoOpinionIsStoredAsNoOpinion:
    def test_no_prediction_at_all_is_stored_as_none(self, stored, scored):
        _run(ml_prob=None)
        assert stored and stored[-1] is None

    def test_a_real_prediction_is_stored_as_is(self, monkeypatch, stored):
        monkeypatch.setattr(ml, "predict", lambda f: 0.42)
        _run(ml_prob=None)
        assert stored[-1] == 0.42
