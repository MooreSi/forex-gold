"""The AI Analysis tab: one call, everything it reads gathered for it.

The NiceGUI page did the gathering inline in the browser layer -- tick,
candles, the last day's Telegram signals, MT5 performance, the strategy
catalogue, H1/M15 candles for volatility and the measured ladder reach -- and
then made ONE model call that answered every metric at once. The React port
never ported the page; its AI tab asks a separate question per subject, and
prints the raw prose each one returns.

This is that gathering, moved where it belongs. What is worth pinning is that
the expensive call happens once, that a piece of evidence that cannot be read
does not cancel the analysis, and that the answer is stored so reopening the
tab does not re-bill.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.ai import market_research as mr


class _Engine:
    """The pieces the research reads off the engine. Nothing reaches a broker."""

    def __init__(self, **broken):
        self.broken = broken
        self.calls = []

    async def _maybe(self, name, value):
        self.calls.append(name)
        if name in self.broken:
            raise RuntimeError(self.broken[name])
        return value

    async def get_tick(self):
        return await self._maybe("tick", {"bid": 2000.0, "ask": 2000.3})

    async def get_candles(self, timeframe, count):
        return await self._maybe(f"candles:{timeframe}", [{"close": 2000.0}])

    async def compute_mt5_performance(self, days):
        return await self._maybe("performance", {"win_rate": 0.5})


@pytest.fixture
def lab(monkeypatch):
    state = {
        "analysis": {"sentiment": "bullish", "summary": "Gold is bid."},
        "calls": 0, "stored": {}, "kwargs": None,
    }

    async def _analyse(**kwargs):
        state["calls"] += 1
        state["kwargs"] = kwargs
        if isinstance(state["analysis"], Exception):
            raise state["analysis"]
        return dict(state["analysis"])

    monkeypatch.setattr(mr._ai, "request_market_analysis", _analyse)
    monkeypatch.setattr(mr, "_load_config", lambda: {"ai_provider": "claude"})
    monkeypatch.setattr(mr, "_recent_signals", lambda cutoff: [{"raw": "BUY"}])
    monkeypatch.setattr(mr, "_ladder_reach", lambda: {"scale_out": {"rungs": 2}})
    monkeypatch.setattr(mr, "_strategy_catalogue", lambda: [{"id": "scale_out"}])
    monkeypatch.setattr(mr._config, "get", lambda k: state["stored"].get(k))
    monkeypatch.setattr(mr._config, "set",
                        lambda k, v: state["stored"].__setitem__(k, v))
    return state


def test_the_model_is_asked_once_for_every_metric(lab):
    """The point of the change: one call, not one per subject."""
    asyncio.run(mr.run_research(_Engine()))

    assert lab["calls"] == 1


def test_the_answer_comes_back_to_the_caller(lab):
    body = asyncio.run(mr.run_research(_Engine()))

    assert body["analysis"]["sentiment"] == "bullish"


def test_everything_the_prompt_reads_is_gathered(lab):
    asyncio.run(mr.run_research(_Engine()))

    kw = lab["kwargs"]
    assert kw["tick"] is not None
    assert kw["candles"]
    assert kw["recent_signals"] == [{"raw": "BUY"}]
    assert kw["performance"] == {"win_rate": 0.5}
    assert kw["strategies"] == [{"id": "scale_out"}]
    assert kw["ladder_reach"] == {"scale_out": {"rungs": 2}}


class TestOnePieceOfEvidenceIsNeverTheWholeAnalysis:
    """Volatility candles, performance and ladder reach are context, not
    preconditions. The NiceGUI page caught each of them separately for
    exactly this reason: a bridge that will not answer for H1 candles must
    not turn into "analysis failed"."""

    def test_missing_performance_still_produces_an_analysis(self, lab):
        body = asyncio.run(mr.run_research(_Engine(performance="no history")))

        assert body["analysis"]["sentiment"] == "bullish"

    def test_missing_volatility_candles_still_produce_an_analysis(self, lab):
        body = asyncio.run(mr.run_research(
            _Engine(**{"candles:H1": "bridge down", "candles:M15": "bridge down"}),
        ))

        assert body["analysis"]["sentiment"] == "bullish"

    def test_a_tick_that_cannot_be_read_still_produces_an_analysis(self, lab):
        body = asyncio.run(mr.run_research(_Engine(tick="bridge down")))

        assert body["analysis"]["sentiment"] == "bullish"


class TestTheAnswerIsKept:
    """Reopening the tab must not re-bill. The NiceGUI page stored the last
    research in app_config and rendered that on open."""

    def test_the_analysis_is_stored(self, lab):
        asyncio.run(mr.run_research(_Engine()))

        assert json.loads(lab["stored"][mr.LAST_RESEARCH_KEY])["data"]["sentiment"] \
            == "bullish"

    def test_it_is_stored_with_when_it_was_run(self, lab):
        asyncio.run(mr.run_research(_Engine()))

        assert json.loads(lab["stored"][mr.LAST_RESEARCH_KEY])["saved_at"]

    def test_the_stored_one_is_read_back_without_asking_the_model(self, lab):
        asyncio.run(mr.run_research(_Engine()))
        lab["calls"] = 0

        body = mr.last_research()

        assert body["analysis"]["sentiment"] == "bullish"
        assert lab["calls"] == 0

    def test_nothing_stored_reads_back_as_nothing_rather_than_raising(self, lab):
        assert mr.last_research()["analysis"] is None

    def test_a_stored_value_that_is_not_json_reads_back_as_nothing(self, lab):
        lab["stored"][mr.LAST_RESEARCH_KEY] = "{not json"

        assert mr.last_research()["analysis"] is None


def test_a_failed_model_call_is_raised_rather_than_stored(lab):
    """A stored failure would be served on every later open as though it were
    an analysis."""
    lab["analysis"] = RuntimeError("model timed out")

    with pytest.raises(RuntimeError):
        asyncio.run(mr.run_research(_Engine()))

    assert lab["stored"] == {}
