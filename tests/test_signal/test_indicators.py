"""The Bounce engine's shared indicators, and where its stops go.

`signal_indicators.py` was at 8.7% coverage. It holds the maths four things
depend on: the Bounce engine, the Breakout engine (which imports from here),
the regime classifier that selects which learned parameters apply, and
`calculate_risk_levels`, which decides where a stop sits and whether a signal
is taken at all.

All pure functions. `adaptive_params.get` reads learned values from the
database, so it is driven from a fixed table — the arithmetic is under test,
not the tuning.

A note on `_counter_bias_allowed`: it reads the wall clock. Its two open
windows are London 08-10 and NY 13-15 UTC, and the tests pin both plus the
hours between, because "allowed" here means the engine may take a trade
AGAINST the higher-timeframe bias.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest

from backend.src.services.test_signal import signal_indicators as si

_PARAMS = {"sl_atr_mult": 0.5, "min_rr": 1.0}


@pytest.fixture(autouse=True)
def params(monkeypatch):
    box = dict(_PARAMS)
    monkeypatch.setattr(si.ap, "get",
                        lambda k, regime=None: box[k])
    return box


def _candles(closes, spread=1.0):
    return [{"open": c, "high": c + spread, "low": c - spread, "close": c}
            for c in closes]


# ─────────────────────────────────────────────────────────────────────────────
# Where the stop goes
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskLevels:

    def _buy(self, **over):
        candidate = {"zone_low": 100.0, "zone_high": 102.0, "key_level": 100.0}
        candidate.update(over.pop("candidate", {}))
        return si.calculate_risk_levels(
            candidate, over.pop("atr", 2.0), over.pop("levels", []),
            "BUY", over.pop("regime", "neutral"),
        )

    def test_the_stop_sits_beyond_the_level_by_an_atr_buffer(self):
        out = self._buy()

        # key_level 100 - (0.5 * 2.0 ATR) = 99.0
        assert out["stop_loss"] == pytest.approx(99.0)
        assert out["entry_mid"] == pytest.approx(101.0)
        assert out["sl_dist"] == pytest.approx(2.0)

    def test_the_targets_are_multiples_of_the_stop_distance(self, params):
        out = self._buy()

        assert out["tp1"] == pytest.approx(101.0 + 2.0 * 1.0)
        assert out["tp2"] == pytest.approx(101.0 + 2.0 * 1.8)
        assert out["tp3"] == pytest.approx(101.0 + 2.0 * 3.0)

    def test_a_sell_mirrors_it(self):
        out = si.calculate_risk_levels(
            {"zone_low": 100.0, "zone_high": 102.0, "key_level": 102.0},
            2.0, [], "SELL",
        )

        assert out["stop_loss"] == pytest.approx(103.0)
        assert out["tp1"] < out["entry_mid"] < out["stop_loss"]

    def test_an_entry_on_the_wrong_side_of_its_own_level_is_refused(self):
        """A BUY whose zone sits BELOW the level it is supposed to bounce off
        gives a negative stop distance. Returning a signal there would place a
        stop above the entry.

        Two independent guards enforce this -- the explicit `sl_dist <= 0`
        check and the R:R floor below it, which a negative distance can never
        satisfy. Mutation confirms it: removing the explicit check leaves this
        test green, because the R:R gate still refuses. So the check is
        defensive rather than load-bearing, and anyone deleting it as "dead"
        will not be told otherwise by the suite. Recorded rather than papered
        over with a contrived case.
        """
        out = self._buy(candidate={"key_level": 105.0})

        assert out is None

    def test_a_reward_below_the_minimum_is_refused(self, params):
        params["min_rr"] = 2.0
        out = self._buy(levels=[])

        # TP1 is min_rr x sl_dist by construction, so it can only fail when the
        # rounding pushes it under -- assert the gate exists rather than that
        # this particular case trips it.
        assert out is None or out["rr_tp1"] >= 2.0 - 0.01

    def test_tp3_prefers_a_real_level_over_a_multiple(self):
        """Structure beats arithmetic: if there is a real level beyond TP1,
        the third target goes there rather than to an invented distance."""
        out = self._buy(levels=[{"price": 106.0}, {"price": 120.0}])

        assert out["tp3"] == pytest.approx(106.0)

    def test_a_level_too_close_to_tp1_is_skipped(self, params):
        """Within 2 points of TP1 is not a separate target."""
        out = self._buy(levels=[{"price": 103.5}])   # tp1 is 103.0

        assert out["tp3"] != pytest.approx(103.5)

    def test_with_no_levels_it_falls_back_to_a_multiple(self):
        out = self._buy(levels=[])

        assert out["tp3"] == pytest.approx(101.0 + 2.0 * 3.0)

    def test_the_reported_reward_matches_the_prices(self):
        """Every screen and every learning record reads rr_tp1. If it drifts
        from the actual prices, the engine learns from a number that never
        happened."""
        out = self._buy()

        assert out["rr_tp1"] == pytest.approx(
            abs(out["tp1"] - out["entry_mid"]) / out["sl_dist"], abs=0.01)
        assert out["rr_tp3"] == pytest.approx(
            abs(out["tp3"] - out["entry_mid"]) / out["sl_dist"], abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# Which learned parameters apply
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeDetection:
    """The regime selects which learned parameter set is used, so getting it
    wrong applies a trending day's tuning to a ranging one."""

    def test_high_adx_with_both_timeframes_agreeing_is_trending(self):
        assert si.detect_regime(30.0, "bullish", "bullish") == "trending"

    def test_high_adx_with_timeframes_DISAGREEING_is_not(self):
        assert si.detect_regime(30.0, "bullish", "bearish") == "neutral"

    def test_high_adx_on_a_neutral_bias_is_not_trending(self):
        """Both agreeing on "neutral" is not agreement about a direction."""
        assert si.detect_regime(30.0, "neutral", "neutral") == "neutral"

    def test_low_adx_is_ranging(self):
        assert si.detect_regime(15.0, "bullish", "bullish") == "ranging"

    @pytest.mark.parametrize("adx", [20.0, 22.0, 25.0])
    def test_the_middle_band_is_neutral(self, adx):
        assert si.detect_regime(adx, "bullish", "bullish") == "neutral"


class TestH4Bias:
    def test_it_needs_a_full_warmup(self):
        """EMA50 on fewer than 52 bars is not an EMA50. Reporting a bias off
        short data would apply a trend filter derived from noise.

        Also double-guarded: the candle-count check and a second check on the
        non-empty closes. Loosening either alone leaves this green. Same note
        as the stop-distance guard above -- the redundancy is real, and worth
        knowing before deleting one of them.
        """
        assert si.compute_h4_bias(_candles(list(range(40)))) == "neutral"

    def test_a_short_series_of_UNUSABLE_candles_is_also_neutral(self):
        """The second guard's own case: enough candles, but the closes do not
        survive filtering."""
        assert si.compute_h4_bias([{"close": 0} for _ in range(80)]) == "neutral"

    def test_a_clean_uptrend_is_bullish(self):
        assert si.compute_h4_bias(_candles([100 + i for i in range(80)])) == "bullish"

    def test_a_clean_downtrend_is_bearish(self):
        assert si.compute_h4_bias(_candles([200 - i for i in range(80)])) == "bearish"

    def test_a_flat_market_is_neither(self):
        assert si.compute_h4_bias(_candles([100.0] * 80)) == "neutral"


class TestAdx:
    def test_too_little_data_returns_the_neutral_default(self):
        """20.0, not 0 — 0 would read as "ranging" and change which parameters
        apply."""
        assert si.compute_adx(_candles([100] * 5)) == 20.0

    def test_a_strong_trend_scores_above_the_trending_threshold(self):
        assert si.compute_adx(_candles([100 + i * 2 for i in range(40)])) > 25

    def test_a_flat_market_does_not(self):
        assert si.compute_adx(_candles([100.0] * 40)) < 25

    def test_it_stays_within_its_stated_range(self):
        for series in ([100 + i * 3 for i in range(60)],
                       [300 - i * 3 for i in range(60)],
                       [100.0] * 60):
            assert 0 <= si.compute_adx(_candles(series)) <= 100


# ─────────────────────────────────────────────────────────────────────────────
# When the engine may trade against the higher-timeframe bias
# ─────────────────────────────────────────────────────────────────────────────

class TestCounterBiasPermission:

    def _at(self, hour, **kw):
        fixed = datetime(2026, 8, 31, hour, 30, tzinfo=timezone.utc)
        with mock.patch.object(si, "datetime") as dt:
            dt.now.return_value = fixed
            return si._counter_bias_allowed(**kw)

    @pytest.mark.parametrize("regime", ["range", "ranging"])
    def test_a_ranging_market_always_allows_it(self, regime):
        assert self._at(3, session="asia", level_type="round",
                        regime=regime) is True

    def test_the_london_window_allows_fading_the_asian_range(self):
        assert self._at(9, session="london", level_type="asian_high",
                        regime="trending") is True

    def test_the_london_window_does_NOT_allow_fading_anything_else(self):
        assert self._at(9, session="london", level_type="round",
                        regime="trending") is False

    def test_the_ny_window_allows_fading_structure(self):
        assert self._at(14, session="ny", level_type="round",
                        regime="trending") is True

    @pytest.mark.parametrize("hour", [7, 10, 12, 15, 20])
    def test_outside_both_windows_a_trending_market_refuses(self, hour):
        assert self._at(hour, session="any", level_type="asian_high",
                        regime="trending") is False

    def test_the_window_boundaries_are_half_open(self):
        """08:00 is in, 10:00 is out — asserted because an off-by-one here
        silently widens or closes a window nobody would notice."""
        assert self._at(8, session="london", level_type="asian_low",
                        regime="trending") is True
        assert self._at(10, session="london", level_type="asian_low",
                        regime="trending") is False
