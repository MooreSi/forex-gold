"""The 3-second velocity path, and every gate that stops it firing.

`breakout_signal_velocity.py` was 14% covered: 97 statements, 83 of them
never executed by a test. It is not a reporting module. `_check_velocity_break`
ends in `_process_candidate(..., velocity=True)`, which is the path to a real
order -- and it fires between M5 candles, so it is the fastest route this
engine has from a price tick to a position.

Ten guards decide whether it fires. Each one is the difference between a
signal and no signal, and until now not one of them was pinned. The tests
below are one guard each, and every one asserts the candidate was NOT
processed, because that is what a guard is for.

NOTHING HERE TOUCHES A BROKER. The bridge, the repo, the adaptive parameters
and `_process_candidate` itself are all stand-ins; `test_no_candidate_is_ever_
really_processed` asserts the stand-in is what ran.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

from backend.src.services.breakout_signal import breakout_signal_velocity as vel


class _Tick:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


class _Bridge:
    def __init__(self, tick):
        self._tick = tick

    async def get_tick(self):
        return self._tick


class _Engine(vel._VelocityMixin):
    """The mixin on a host with only what it reaches for."""

    def __init__(self, tick, cached, history):
        self.is_running = True
        self._bridge = _Bridge(tick)
        self._cached = cached
        self._price_history = list(history)
        self._velocity_cooldowns: dict = {}
        self.processed: list = []

    async def _process_candidate(self, candidate, context, *args, **kwargs):
        self.processed.append((candidate, context, kwargs))


# Parameter defaults that let a break through, so each test turns exactly one
# thing off and nothing else can be blamed for the result.
_PARAMS = {
    "min_break_pts": 2.0,
    "min_adx_go": 25.0,
    "max_adx_entry": 50.0,
    "require_dual_bias": 0.0,
    "compression_max_range_atr": 1.0,
}

_CACHED = {
    "key_levels": [{"price": 4000.0, "type": "swing_high", "strength": 2}],
    "adx": 30.0,
    "htf_bias": "bullish",
    "h4_bias": "bullish",
    "atr": 10.0,
    "macd_hist": 0.5,
    "m5_candles": [{"high": 4001.0, "low": 3999.0}],
}

_NOW = 1_789_000_000.0

# Four samples inside the 20-second window, all below the level, so the last
# price crossing above it is a genuine break rather than a gap in the data.
_HISTORY = [(_NOW - 12, 3995.0), (_NOW - 9, 3996.0),
            (_NOW - 6, 3997.0), (_NOW - 3, 3998.0)]


@pytest.fixture
def lab(monkeypatch):
    """Every collaborator replaced, and the clock pinned."""
    state = {
        "params": dict(_PARAMS),
        "cached": {k: (list(v) if isinstance(v, list) else v)
                   for k, v in _CACHED.items()},
        "open_signals": [],
        "session": "london",
        "session_active": True,
        "news": False,
        "compressed": True,
        "tick": _Tick(4002.5, 4003.5),
        "history": list(_HISTORY),
    }

    monkeypatch.setattr(vel.ap, "get", lambda key: state["params"][key])
    monkeypatch.setattr(vel.bdb, "get_open_signals", lambda: state["open_signals"])
    monkeypatch.setattr(vel, "get_session", lambda: state["session"])
    monkeypatch.setattr(vel, "session_is_active", lambda s: state["session_active"])
    monkeypatch.setattr(vel, "is_news_window", lambda: state["news"])
    monkeypatch.setattr(vel, "_is_compressed", lambda *a, **k: state["compressed"])
    monkeypatch.setattr(vel.time, "time", lambda: _NOW)
    return state


def _run(state) -> _Engine:
    engine = _Engine(state["tick"], state["cached"], state["history"])
    asyncio.run(engine._check_velocity_break())
    return engine


# ── The guard rail on the guard rails ────────────────────────────────────────

def test_no_candidate_is_ever_really_processed(lab):
    """Asserted, not assumed: `_process_candidate` here is the stand-in."""
    engine = _run(lab)

    assert type(engine)._process_candidate is not vel._VelocityMixin.__dict__.get(
        "_process_candidate", None)
    assert all(isinstance(c, dict) for c, _ctx, _kw in engine.processed)


def test_a_clean_break_does_fire(lab):
    """The positive control. Without it every guard test below would pass on a
    path that never fires for some unrelated reason."""
    engine = _run(lab)

    assert len(engine.processed) == 1
    candidate, context, kwargs = engine.processed[0]
    assert candidate["direction"] == "BUY"
    assert candidate["trigger"] == "velocity"
    assert kwargs["velocity"] is True
    # The ASK for a buy: the side that will actually be paid.
    assert context["price"] == pytest.approx(4003.5)


# ── The gates, one test each ─────────────────────────────────────────────────

def test_it_does_not_fire_with_no_key_levels(lab):
    lab["cached"]["key_levels"] = []

    assert _run(lab).processed == []


def test_it_does_not_fire_before_adx_has_been_measured(lab):
    # 0 is "not computed yet", not "a flat market".
    lab["cached"]["adx"] = 0.0

    assert _run(lab).processed == []


def test_it_does_not_fire_outside_an_active_session(lab):
    lab["session_active"] = False

    assert _run(lab).processed == []


def test_it_never_fires_in_the_asian_session(lab):
    # Excluded outright, not by the session-active check: thin liquidity makes
    # a three-second price move a different thing from a breakout.
    lab["session"] = "asian"

    assert _run(lab).processed == []


def test_it_does_not_fire_inside_a_news_window(lab):
    lab["news"] = True

    assert _run(lab).processed == []


def test_it_does_not_fire_with_two_signals_already_open(lab):
    lab["open_signals"] = [{"direction": "BUY"}, {"direction": "SELL"}]

    assert _run(lab).processed == []


def test_two_open_signals_block_the_side_that_is_still_free(lab):
    """The exposure cap, isolated from the one-per-direction rule.

    The test above it looks like it pins the `>= 2` cap and does not: with a
    BUY and a SELL open, the per-direction checks further down block both
    sides on their own, so deleting the cap entirely leaves that test green.
    Found by mutation on 2026-09-19 -- the mutant survived.

    Two SELLs leave the BUY side free of the per-direction rule, so the cap
    is the only thing standing between this tick and a third position.
    """
    lab["open_signals"] = [{"direction": "SELL"}, {"direction": "SELL"}]

    assert _run(lab).processed == []


def test_it_does_not_fire_when_the_bridge_has_no_price(lab):
    lab["tick"] = None

    assert _run(lab).processed == []


def test_it_does_not_fire_on_too_few_samples(lab):
    # Fewer than four points in twenty seconds is not a velocity measurement.
    lab["history"] = _HISTORY[:2]

    assert _run(lab).processed == []


def test_it_does_not_fire_below_the_adx_floor(lab):
    lab["cached"]["adx"] = 20.0

    assert _run(lab).processed == []


def test_it_does_not_fire_above_the_adx_ceiling(lab):
    # An exhausted trend, not an igniting one.
    lab["cached"]["adx"] = 60.0

    assert _run(lab).processed == []


def test_it_does_not_fire_in_a_quiet_market(lab):
    # ATR below 7 means the "break" is inside the noise.
    lab["cached"]["atr"] = 6.9

    assert _run(lab).processed == []


def test_it_does_not_fire_without_prior_compression(lab):
    lab["compressed"] = False

    assert _run(lab).processed == []


def test_it_does_not_fire_against_the_higher_timeframe_bias(lab):
    lab["cached"]["htf_bias"] = "bearish"

    assert _run(lab).processed == []


def test_it_does_not_fire_against_macd(lab):
    lab["cached"]["macd_hist"] = -0.5

    assert _run(lab).processed == []


def test_it_does_not_fire_when_price_was_already_above_the_level(lab):
    # Nothing was crossed. This is the difference between a breakout and a
    # price that was simply up there already.
    lab["history"] = [(_NOW - 12, 4001.0), (_NOW - 9, 4001.5),
                      (_NOW - 6, 4002.0), (_NOW - 3, 4002.2)]

    assert _run(lab).processed == []


def test_it_does_not_fire_before_price_clears_the_level_by_the_minimum(lab):
    # 4000 + 2.0 minimum = 4002.0; a mid of 4001.5 has not cleared it.
    lab["tick"] = _Tick(4001.0, 4002.0)

    assert _run(lab).processed == []


class TestTheDualBiasSwitch:

    def test_with_it_off_the_h4_bias_is_ignored(self, lab):
        lab["params"]["require_dual_bias"] = 0.0
        lab["cached"]["h4_bias"] = "bearish"

        assert len(_run(lab).processed) == 1

    def test_with_it_on_a_disagreeing_h4_bias_blocks(self, lab):
        lab["params"]["require_dual_bias"] = 1.0
        lab["cached"]["h4_bias"] = "bearish"

        assert _run(lab).processed == []

    def test_a_neutral_h4_bias_is_not_a_disagreement(self, lab):
        # It matters: bugs/060 recorded that h4_bias has been "neutral" on
        # every signal this engine has ever produced. If neutral blocked, the
        # switch would silently disable the whole velocity path.
        lab["params"]["require_dual_bias"] = 1.0
        lab["cached"]["h4_bias"] = "neutral"

        assert len(_run(lab).processed) == 1


class TestTheCooldown:

    def test_the_same_level_does_not_fire_twice_inside_the_cooldown(self, lab):
        engine = _Engine(lab["tick"], lab["cached"], lab["history"])
        engine._velocity_cooldowns["BUY:4000"] = _NOW - 10

        asyncio.run(engine._check_velocity_break())

        assert engine.processed == []

    def test_it_fires_again_once_the_cooldown_has_passed(self, lab):
        engine = _Engine(lab["tick"], lab["cached"], lab["history"])
        engine._velocity_cooldowns["BUY:4000"] = _NOW - 91

        asyncio.run(engine._check_velocity_break())

        assert len(engine.processed) == 1

    def test_firing_records_the_cooldown(self, lab):
        engine = _run(lab)

        assert engine._velocity_cooldowns["BUY:4000"] == _NOW

    def test_the_cooldown_is_per_direction(self, lab):
        # A SELL cooldown must not silence a BUY at the same level.
        engine = _Engine(lab["tick"], lab["cached"], lab["history"])
        engine._velocity_cooldowns["SELL:4000"] = _NOW

        asyncio.run(engine._check_velocity_break())

        assert len(engine.processed) == 1


class TestOnePerDirection:

    def test_an_open_buy_blocks_another_buy(self, lab):
        lab["open_signals"] = [{"direction": "BUY"}]

        assert _run(lab).processed == []

    def test_an_open_sell_does_not_block_a_buy(self, lab):
        lab["open_signals"] = [{"direction": "SELL"}]

        assert len(_run(lab).processed) == 1


class TestTheSellSide:

    @pytest.fixture(autouse=True)
    def _falling(self, lab):
        lab["cached"]["htf_bias"] = "bearish"
        lab["cached"]["h4_bias"] = "bearish"
        lab["cached"]["macd_hist"] = -0.5
        lab["history"] = [(_NOW - 12, 4005.0), (_NOW - 9, 4004.0),
                          (_NOW - 6, 4003.0), (_NOW - 3, 4002.0)]
        lab["tick"] = _Tick(3996.5, 3997.5)
        return lab

    def test_a_clean_break_down_fires(self, lab):
        engine = _run(lab)

        assert len(engine.processed) == 1
        assert engine.processed[0][0]["direction"] == "SELL"

    def test_it_quotes_the_bid_for_a_sell(self, lab):
        # The side that will actually be paid.
        _candidate, context, _kw = _run(lab).processed[0]

        assert context["price"] == pytest.approx(3996.5)

    def test_it_reports_how_far_beyond_the_level_price_went(self, lab):
        candidate, _ctx, _kw = _run(lab).processed[0]

        # mid 3997.0 against a level of 4000.0.
        assert candidate["pts_beyond"] == pytest.approx(3.0)

    def test_an_open_sell_blocks_another_sell(self, lab):
        lab["open_signals"] = [{"direction": "SELL"}]

        assert _run(lab).processed == []


class TestTheRollingWindow:

    def test_a_sample_older_than_the_window_is_dropped(self, lab):
        # 25 seconds ago, outside the 20-second window: if it were kept, the
        # "oldest price" would be one from before the move started and every
        # break would look bigger than it was.
        lab["history"] = [(_NOW - 25, 3000.0)] + list(_HISTORY)

        engine = _run(lab)

        assert all(t >= _NOW - 20 for t, _p in engine._price_history)

    def test_the_current_price_is_added_to_the_history(self, lab):
        engine = _run(lab)

        assert engine._price_history[-1] == (_NOW, pytest.approx(4003.0))
