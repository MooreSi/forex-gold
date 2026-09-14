"""The indicators the Breakout engine's maths rests on.

`compute_h4_bias`, `compute_adx`, `compute_macd_hist` and `detect_regime`.
Between them they set the higher-timeframe bias, measure trend strength and
pick the regime that selects which learned parameters apply.

All pure functions -- candles or closes in, a number or a word out.

**Ported 2026-09-14 from `tests/test_signal/test_indicators.py`**, which died
with the Bounce engine (`docs/todo/bugs/046`). These four functions did not:
they were never Bounce-specific, the Breakout engine imported them across the
package boundary, and they moved to `services/market/indicators.py`. The
assertions below are unchanged.

What did NOT come with them, because it went with the engine:

  * `TestRiskLevels` -- `calculate_risk_levels` read the Bounce engine's
    adaptive parameters to place a stop, and there is no such store any more.
  * `TestCounterBiasPermission` -- `_counter_bias_allowed` was consulted only
    by `check_scalp_trigger`, which was the Bounce engine's M5 entry.

Both are a real coverage loss on code that no longer exists, which is the only
kind worth taking.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import indicators as si


def _candles(closes, spread=1.0):
    return [{"open": c, "high": c + spread, "low": c - spread, "close": c}
            for c in closes]


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
