"""The Asian trend-gate exemption, exercised on the path that places orders.

**No test here reaches a broker.** `_main_eng` is None throughout, so a
signal that got past every gate would raise rather than quietly place
anything, and `re_db.update_live_exec` is replaced by a recorder. The same
harness as `test_new_gates_are_wired.py`, for the same reason.

The switch, the measurement behind it and the design argument are in
`tests/risk/test_htf_bias_gate_asian_exemption.py`. What is proved HERE is
the one property that file cannot check by reading source text: the
exemption stands down **both** of this path's counter-bias rules.

There are two, and while the trend gate is on the second is invisible:

  1. `_gov.htf_bias_blocks` -- the owner's gate, which ignores level_score;
  2. the original `level_score < 0.75` bypass (reversal-engine/090), which
     refuses a counter-bias signal only on a weakly-scored level.

(1) fires for every counter-bias signal, so it already covers every case
(2) would have caught. Stand (1) down in Asia and (2) becomes load-bearing
again -- and the switch would silently mean "counter-bias in Asia, but only
on levels scoring 0.75 or better", which is not what it says and not what
was measured. A source-shape assertion cannot see that; only running the
branch can. It was a live survivor in the first mutation pass on this
change, which is why this file exists.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.reversal_engine.reversal_engine_live_execute import (
    _LiveExecuteMixin)
from backend.src.services.reversal_engine.reversal_engine_service import (
    ReversalEngine as _ReversalEngine)


def _falling(n=10):
    """Enough H1 candles for `get_htf_bias` to decide BEARISH: lower highs
    and lower lows across the two halves, read from closes."""
    return [{"ts": i, "open": 3400 - i, "high": 3401 - i,
             "low": 3399 - i, "close": 3400 - i} for i in range(n)]


class _Bridge:
    def __init__(self, candles):
        self._candles = candles

    async def get_candles(self, timeframe, count):
        return list(self._candles)


class _Engine(_LiveExecuteMixin):
    """The mixin as ReversalEngine actually composes it.

    `_calc_atr` and `_calc_adx` are borrowed from the real service rather
    than stubbed: the fill-time re-evaluation calls them, and a stub that
    happened to raise would send the whole block down its
    `except`-and-fall-back branch, skipping the bias check entirely. That
    is not hypothetical -- the first version of this file did exactly that
    and every refusal test passed for the wrong reason."""

    _calc_atr = _ReversalEngine.__dict__["_calc_atr"]
    _calc_adx = _ReversalEngine.__dict__["_calc_adx"]

    def __init__(self, bridge):
        self._main_eng = None
        self._bridge = bridge


@pytest.fixture
def statuses(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute"
        ".re_db.update_live_exec",
        lambda sig_id, status=None, **kw: seen.__setitem__(sig_id, status))
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute"
        ".re_db.store_ml_prob_at_fill",
        lambda *a, **kw: None)
    return seen


@pytest.fixture
def clear_path(monkeypatch):
    monkeypatch.setattr("backend.src.services.risk.schedule.check_trading_schedule",
                        lambda **kw: (True, ""))
    monkeypatch.setattr("backend.src.utils.news_calendar.check_news_blackout",
                        lambda: (True, ""))


@pytest.fixture
def in_session(monkeypatch):
    """Pin the session the path reads, so these tests do not depend on the
    hour they run at. `get_session` itself is covered in
    tests/reversal_engine/test_level_detector.py."""
    def _set(name):
        monkeypatch.setattr(
            "backend.src.services.reversal_engine.reversal_engine_live_execute"
            ".ld.get_session", lambda hour: name)
    return _set


def _settings(monkeypatch, **extra):
    rs = {"re_live_execution": 1, "strategy_lot_size": 0.01}
    rs.update(extra)
    monkeypatch.setattr("backend.src.db.database.get_risk_settings", lambda: rs)
    return rs


def _run(engine, level_score):
    """A BUY into a falling market -- counter-bias by construction."""
    sig = {"id": 1, "signal_ref": "RE-1", "direction": "BUY",
           "level_price": 3300.0, "atr": 8.0, "level_score": level_score}
    return asyncio.run(engine._try_live_execute(sig, 3300.0, None))


# 0.60 is refused by BOTH rules; 0.95 only by the owner's gate. Running each
# test at both is what separates "the exemption works" from "the exemption
# works on strong levels and silently does nothing on weak ones".
_SCORES = [0.60, 0.95]


class TestWithoutTheExemption:
    """Today's behaviour, and what must not change when the switch is off."""

    @pytest.mark.parametrize("score", _SCORES)
    def test_a_counter_bias_buy_in_asia_is_refused(self, monkeypatch, statuses,
                                                   clear_path, in_session, score):
        in_session("asian")
        _settings(monkeypatch, htf_bias_gate_enabled=1)
        _run(_Engine(_Bridge(_falling())), score)
        assert statuses.get(1) == "bias_skipped"

    @pytest.mark.parametrize("score", _SCORES)
    def test_the_column_absent_behaves_the_same(self, monkeypatch, statuses,
                                                clear_path, in_session, score):
        """An install that has not run migration 45."""
        in_session("asian")
        _settings(monkeypatch, htf_bias_gate_enabled=1)
        _run(_Engine(_Bridge(_falling())), score)
        assert statuses.get(1) == "bias_skipped"


class TestWithTheExemptionOn:
    @pytest.mark.parametrize("score", _SCORES)
    def test_a_counter_bias_buy_in_asia_is_allowed_through(
            self, monkeypatch, statuses, clear_path, in_session, score):
        """The point of the whole change, asserted at BOTH level scores.

        At 0.95 this passes even if only the owner's gate was exempted. At
        0.60 it passes only if the level-score bypass was exempted too, and
        that is the assertion a mutation to the second clause survives
        without."""
        in_session("asian")
        _settings(monkeypatch, htf_bias_gate_enabled=1, htf_bias_asian_exempt=1)
        _run(_Engine(_Bridge(_falling())), score)
        assert statuses.get(1) != "bias_skipped"

    @pytest.mark.parametrize("session", ["london", "overlap", "ny", "off"])
    @pytest.mark.parametrize("score", _SCORES)
    def test_outside_asia_it_is_still_refused(self, monkeypatch, statuses,
                                              clear_path, in_session,
                                              session, score):
        in_session(session)
        _settings(monkeypatch, htf_bias_gate_enabled=1, htf_bias_asian_exempt=1)
        _run(_Engine(_Bridge(_falling())), score)
        assert statuses.get(1) == "bias_skipped"

    def test_with_the_trend_gate_off_the_old_bypass_still_refuses(
            self, monkeypatch, statuses, clear_path, in_session):
        """The exemption relaxes the owner's gate. With that gate off there
        is nothing to relax, and the level-score bypass -- which predates
        this change -- must keep refusing exactly as it does today. An
        exemption that ignored the gate's own switch would turn this green
        and quietly disable a rule nobody asked about."""
        in_session("asian")
        _settings(monkeypatch, htf_bias_gate_enabled=0, htf_bias_asian_exempt=1)
        _run(_Engine(_Bridge(_falling())), 0.60)
        assert statuses.get(1) == "bias_skipped"


class TestTheHarnessCanSeeARefusalAtAll:
    """Negative control. Every assertion above compares against the string
    "bias_skipped"; if the path never wrote a status, half of them would
    pass for the wrong reason."""

    def test_a_with_bias_signal_is_not_refused_by_this_branch(
            self, monkeypatch, statuses, clear_path, in_session):
        in_session("asian")
        _settings(monkeypatch, htf_bias_gate_enabled=1)
        sig = {"id": 1, "signal_ref": "RE-1", "direction": "SELL",
               "level_price": 3300.0, "atr": 8.0, "level_score": 0.60}
        asyncio.run(_Engine(_Bridge(_falling()))._try_live_execute(
            sig, 3300.0, None))
        assert statuses.get(1) != "bias_skipped"

    def test_the_candles_really_do_read_bearish(self):
        """If `_falling` stopped producing a decided bias, every refusal
        test above would pass with the gate doing nothing."""
        from backend.src.services.reversal_engine import level_detector as ld

        assert ld.get_htf_bias(_falling(), _falling()) == "bearish"
