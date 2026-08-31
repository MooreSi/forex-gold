"""The rules that decide whether a breakout is taken, and where its stop goes.

`breakout_signal/signal_generator.py` was at 6.7% coverage. Everything tested
here is a pure function — no broker, no database, no engine — and each one
encodes a decision that was arrived at from live losses, with the reasoning in
its docstring. That makes them exactly the kind of thing that gets "simplified"
by someone who does not know what the numbers cost.

  * `_strong_close` — a break that closes with a long rejection wick back
    through the level is a failed break.
  * `_is_compressed` — a genuine breakout comes out of a squeeze; a "break"
    mid-run is continuation-chasing, the pattern behind most of this engine's
    recorded losses.
  * `_adx_rising` — a break on falling ADX tends to fade. Note it returns True
    on insufficient history: it is a filter, and a filter that blocks when it
    cannot judge is a filter that stops the engine trading at all.
  * `calculate_breakout_risk_levels` — the stop floor exists because the
    recorded average stop was 0.61×ATR and 85% of losses stopped out within
    fifteen minutes. A stop that cannot survive one ordinary M5 candle is not a
    stop.

`adaptive_params.get` reads from the database, so it is driven from a fixed
table here — the arithmetic is what is under test, not the tuning.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import signal_generator as sg

_PARAMS = {
    "sl_atr_mult": 0.5,
    "sweep_sl_atr_mult": 0.3,
    "min_sl_dist_atr": 1.0,
    "max_sl_dist_atr": 2.5,
    "tp1_mult": 1.0,
    "tp2_mult": 1.8,
    "tp3_mult": 2.8,
}


@pytest.fixture(autouse=True)
def params(monkeypatch):
    box = dict(_PARAMS)
    monkeypatch.setattr(sg.ap, "get", lambda k: box[k])
    return box


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class TestAStrongCloseMeansNoRejectionWick:
    """Outer 35% of the candle's range, in the direction of the break."""

    def test_a_buy_closing_at_the_high_is_strong(self):
        assert sg._strong_close(_candle(100, 110, 100, 110), "BUY") is True

    def test_a_buy_closing_at_the_low_is_not(self):
        assert sg._strong_close(_candle(100, 110, 100, 100), "BUY") is False

    def test_a_sell_closing_at_the_low_is_strong(self):
        assert sg._strong_close(_candle(110, 110, 100, 100), "SELL") is True

    def test_a_sell_closing_at_the_high_is_not(self):
        assert sg._strong_close(_candle(100, 110, 100, 110), "SELL") is False

    @pytest.mark.parametrize("close,expected", [
        (106.4, False),   # 64% of the range — just inside
        (106.5, True),    # 65% — the boundary itself passes
        (107.0, True),
    ])
    def test_the_boundary_is_at_65_percent_for_a_buy(self, close, expected):
        assert sg._strong_close(_candle(100, 110, 100, close), "BUY") is expected

    def test_a_zero_range_candle_is_never_strong(self):
        """A doji at the level tells you nothing, and dividing by its range
        would raise."""
        assert sg._strong_close(_candle(100, 100, 100, 100), "BUY") is False


class TestCompressionIsRequiredBeforeABreak:

    def test_a_tight_window_is_compressed(self):
        candles = [_candle(100, 101, 99, 100) for _ in range(12)]

        assert sg._is_compressed(candles, atr=2.0, max_range_atr=1.5) is True

    def test_a_wide_window_is_not(self):
        """The continuation-chase case: price already ran, so the window's span
        is far wider than one ATR."""
        candles = [_candle(100 + i, 101 + i, 99 + i, 100 + i) for i in range(12)]

        assert sg._is_compressed(candles, atr=2.0, max_range_atr=1.5) is False

    def test_too_few_candles_is_not_compressed(self):
        candles = [_candle(100, 101, 99, 100) for _ in range(5)]

        assert sg._is_compressed(candles, atr=2.0, max_range_atr=1.5) is False

    def test_a_zero_atr_is_not_compressed(self):
        """Guard, not a judgement: with no volatility measure the ratio is
        meaningless, and 0 would make every window look tight."""
        candles = [_candle(100, 101, 99, 100) for _ in range(12)]

        assert sg._is_compressed(candles, atr=0.0, max_range_atr=1.5) is False

    def test_only_the_last_window_counts(self):
        """Older, wider candles must not disqualify a window that has since
        settled — otherwise nothing ever looks compressed after a big move."""
        wide = [_candle(100 + i * 5, 105 + i * 5, 95 + i * 5, 100 + i * 5)
                for i in range(12)]
        tight = [_candle(200, 201, 199, 200) for _ in range(12)]

        assert sg._is_compressed(wide + tight, atr=2.0, max_range_atr=1.5) is True


class TestTheAdxFilterFailsOPEN:
    """It is a filter. One that blocks when it cannot judge stops the engine
    trading at all, which is a much worse failure than letting a weak setup
    through."""

    def test_not_enough_history_does_not_block(self):
        assert sg._adx_rising([_candle(100, 101, 99, 100)] * 10, adx_now=20.0) is True

    def test_a_broken_adx_computation_does_not_block(self, monkeypatch):
        def _boom(_c):
            raise ValueError("bad candles")
        monkeypatch.setattr(sg, "compute_adx", _boom)

        assert sg._adx_rising([_candle(100, 101, 99, 100)] * 40, adx_now=20.0) is True

    def test_rising_adx_passes(self, monkeypatch):
        monkeypatch.setattr(sg, "compute_adx", lambda _c: 20.0)

        assert sg._adx_rising([_candle(100, 101, 99, 100)] * 40, adx_now=25.0) is True

    def test_falling_adx_is_blocked(self, monkeypatch):
        monkeypatch.setattr(sg, "compute_adx", lambda _c: 30.0)

        assert sg._adx_rising([_candle(100, 101, 99, 100)] * 40, adx_now=25.0) is False

    def test_a_flat_adx_is_blocked_too(self, monkeypatch):
        """The threshold is +0.25, not "any increase" — noise is not a trend."""
        monkeypatch.setattr(sg, "compute_adx", lambda _c: 25.0)

        assert sg._adx_rising([_candle(100, 101, 99, 100)] * 40, adx_now=25.1) is False


class TestTheBodyMustCloseBeyondTheLevel:
    """A wick through the level is a test of it, not a break of it."""

    def test_a_buy_body_beyond_resistance_counts(self):
        assert sg._body_beyond_level(_candle(99, 106, 98, 105), 100, "BUY") == 5

    def test_a_buy_wick_through_resistance_does_not(self):
        assert sg._body_beyond_level(_candle(98, 106, 97, 99), 100, "BUY") == 0

    def test_a_sell_body_beyond_support_counts(self):
        assert sg._body_beyond_level(_candle(101, 102, 94, 95), 100, "SELL") == 5

    def test_a_sell_wick_through_support_does_not(self):
        assert sg._body_beyond_level(_candle(102, 103, 94, 101), 100, "SELL") == 0


class TestTheStopIsNeverTooTightOrTooFar:

    def _levels(self, **over):
        candidate = {"direction": "BUY", "broken_level": 100.0}
        candidate.update(over.pop("candidate", {}))
        return sg.calculate_breakout_risk_levels(
            candidate, over.pop("price", 101.0), over.pop("atr", 1.0),
            over.pop("adx", 25.0),
        )

    def test_an_ordinary_break_produces_a_full_set(self):
        out = self._levels()

        assert out is not None
        assert out["stop_loss"] < out["entry_mid"] < out["tp1"] < out["tp2"] < out["tp3"]
        assert out["rr_tp1"] >= 0.85

    def test_a_stop_tighter_than_the_floor_is_pushed_out(self, params):
        """0.61×ATR was the recorded average, and 85% of losses stopped out
        within fifteen minutes. The floor is 1.0×ATR here."""
        out = self._levels(price=100.2, atr=1.0)

        assert out is not None
        assert out["sl_dist"] == pytest.approx(1.0), (
            "the stop was left inside the floor -- it cannot survive one "
            "ordinary candle"
        )
        assert out["stop_loss"] == pytest.approx(99.2)

    def test_a_stop_beyond_the_cap_is_REFUSED_not_clamped(self, params):
        """Being far from the level means chasing. Clamping would take the
        trade anyway at a stop that no longer sits beyond the structure."""
        out = self._levels(price=105.0, atr=1.0)

        assert out is None

    def test_the_sell_side_mirrors_it(self):
        out = sg.calculate_breakout_risk_levels(
            {"direction": "SELL", "broken_level": 100.0}, 99.0, 1.0, 25.0)

        assert out is not None
        assert out["stop_loss"] > out["entry_mid"] > out["tp1"] > out["tp2"] > out["tp3"]

    def test_a_poor_reward_to_risk_is_refused(self, params):
        params["tp1_mult"] = 0.5

        assert self._levels() is None

    def test_the_boundary_reward_is_accepted(self, params):
        """0.85 is the minimum, and TP1 is a partial take-profit rather than
        the only exit -- so it may sit slightly under 1R."""
        params["tp1_mult"] = 0.85

        out = self._levels()

        assert out is not None and out["rr_tp1"] == 0.85


class TestASweepStopsBeyondTheWickNotTheLevel:
    """The wick already marks where the liquidity grab exhausted, so a revisit
    of that extreme invalidates the reversal. Tighter buffer than a break."""

    def test_it_anchors_on_the_wick(self, params):
        out = sg.calculate_breakout_risk_levels(
            {"direction": "BUY", "broken_level": 100.0,
             "breakout_type": "sweep", "wick_extreme": 99.5},
            101.0, 1.0, 25.0,
        )

        assert out is not None
        # wick 99.5 - (0.3 * 1.0) = 99.2, which is 1.8 ATR from entry -- inside
        # the 2.5 cap. A wick further out than that is refused by the cap, the
        # same as any other over-extended entry.
        assert out["stop_loss"] == pytest.approx(99.2)

    def test_a_break_of_the_same_shape_anchors_on_the_level_instead(self, params):
        """Control: same numbers, no sweep, so the level and the wider buffer
        apply."""
        out = sg.calculate_breakout_risk_levels(
            {"direction": "BUY", "broken_level": 100.0, "wick_extreme": 99.5},
            101.0, 1.0, 25.0,
        )

        assert out is not None
        assert out["stop_loss"] == pytest.approx(99.5)   # level - 0.5*ATR

    def test_a_sweep_wick_too_far_out_is_still_refused_by_the_cap(self, params):
        """The tighter sweep buffer is not an exemption from the distance cap.
        My first draft of the test above used a wick 3 ATR below entry and got
        None -- correctly."""
        out = sg.calculate_breakout_risk_levels(
            {"direction": "BUY", "broken_level": 100.0,
             "breakout_type": "sweep", "wick_extreme": 98.0},
            101.0, 1.0, 25.0,
        )

        assert out is None
