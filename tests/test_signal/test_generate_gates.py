"""The Bounce engine's three counter-trend gates, as behaviour rather than text.

These three rules decide whether the Bounce engine is allowed to act on a
trigger it has already found. Until now they were inline boolean expressions
in the middle of `TestSignalEngine.generate`, a method that cannot be called
without candles, a bridge and a database -- so nothing tested them, and the
package sits at 25.6% coverage.

That matters right now for one of them in particular. The Asian-session rule
here says the opposite of what the Reversal Engine's own numbers say about the
same hours (`docs/simon-handover/033`): this engine refuses counter-bias
signals between 00:00 and 07:00 UTC, and it has done since before anything was
measured. Whichever way the owner decides that, the rule should be readable and
pinned first -- an unmeasured rule that nothing tests is the worst of both.

Nothing here changes behaviour. The last class proves that: it evaluates the
original inline expressions, copied verbatim from the source before the
extraction, against the extracted functions over every combination of their
inputs.
"""
from __future__ import annotations

import itertools

from backend.src.services.test_signal._gates import (
    asian_counter_bias_blocks,
    dual_bias_blocks,
    extreme_trend_blocks,
)


class TestTheAsianSessionRule:
    def test_a_counter_bias_signal_in_the_asian_session_is_refused(self):
        assert asian_counter_bias_blocks(
            session="asian", direction="BUY", htf_bias="bearish",
            trigger_pattern="bounce",
        )

    def test_a_trend_aligned_signal_in_the_asian_session_is_allowed(self):
        assert asian_counter_bias_blocks(
            session="asian", direction="SELL", htf_bias="bearish",
            trigger_pattern="bounce",
        ) is None

    def test_the_same_counter_bias_signal_at_any_other_hour_is_allowed(self):
        """The rule is the session, not the direction. This is the cell
        docs/simon-handover/033 is about."""
        assert asian_counter_bias_blocks(
            session="london", direction="BUY", htf_bias="bearish",
            trigger_pattern="bounce",
        ) is None

    def test_a_neutral_bias_has_no_counter_side(self):
        assert asian_counter_bias_blocks(
            session="asian", direction="BUY", htf_bias="neutral",
            trigger_pattern="bounce",
        ) is None

    def test_a_liquidity_sweep_is_exempt(self):
        """A sweep is the one pattern whose whole premise is trading against
        the crowd, so the counter-trend gates let it through. The extreme-trend
        gate below does not -- that asymmetry is real and is pinned there."""
        assert asian_counter_bias_blocks(
            session="asian", direction="BUY", htf_bias="bearish",
            trigger_pattern="liquidity_sweep",
        ) is None

    def test_the_refusal_says_which_rule_and_which_way(self):
        """The string reaches `_status_detail` and the analysis log, and
        bugs/038 was a refusal naming the wrong rule."""
        reason = asian_counter_bias_blocks(
            session="asian", direction="BUY", htf_bias="bearish",
            trigger_pattern="bounce",
        )

        assert reason == (
            "Asian counter-bias block: HTF bearish, signal BUY — "
            "only trend-aligned signals in Asian session"
        )


class TestTheDualBiasRule:
    def test_counter_trend_is_refused_when_both_timeframes_agree_and_adx_is_high(self):
        assert dual_bias_blocks(
            direction="SELL", htf_bias="bullish", h4_bias="bullish", adx=30.0,
            trigger_pattern="bounce", threshold=25.0,
        )

    def test_it_does_not_fire_when_the_two_timeframes_disagree(self):
        assert dual_bias_blocks(
            direction="SELL", htf_bias="bullish", h4_bias="bearish", adx=30.0,
            trigger_pattern="bounce", threshold=25.0,
        ) is None

    def test_it_does_not_fire_below_the_adx_threshold(self):
        assert dual_bias_blocks(
            direction="SELL", htf_bias="bullish", h4_bias="bullish", adx=24.9,
            trigger_pattern="bounce", threshold=25.0,
        ) is None

    def test_the_threshold_itself_blocks(self):
        """`>=`, not `>`. A tunable whose boundary moves by one tick when
        someone rewrites the comparison is a silent behaviour change."""
        assert dual_bias_blocks(
            direction="SELL", htf_bias="bullish", h4_bias="bullish", adx=25.0,
            trigger_pattern="bounce", threshold=25.0,
        )

    def test_a_trend_aligned_signal_is_allowed_however_strong_the_trend(self):
        assert dual_bias_blocks(
            direction="BUY", htf_bias="bullish", h4_bias="bullish", adx=60.0,
            trigger_pattern="bounce", threshold=25.0,
        ) is None

    def test_a_liquidity_sweep_is_exempt(self):
        assert dual_bias_blocks(
            direction="SELL", htf_bias="bullish", h4_bias="bullish", adx=60.0,
            trigger_pattern="liquidity_sweep", threshold=25.0,
        ) is None

    def test_a_neutral_bias_has_no_counter_side(self):
        assert dual_bias_blocks(
            direction="SELL", htf_bias="neutral", h4_bias="neutral", adx=60.0,
            trigger_pattern="bounce", threshold=25.0,
        ) is None


class TestTheExtremeTrendRule:
    def test_mean_reversion_patterns_are_refused_in_a_persistent_trend(self):
        assert extreme_trend_blocks(
            htf_bias="bullish", h4_bias="bullish", adx=45.0,
            trigger_pattern="bounce", threshold=40.0,
        )

    def test_it_refuses_a_liquidity_sweep_too(self):
        """The asymmetry worth knowing: the other two gates exempt a sweep and
        this one does not. Levels stop holding in a persistent trend, which is
        the premise a sweep depends on as much as a bounce does."""
        assert extreme_trend_blocks(
            htf_bias="bullish", h4_bias="bullish", adx=45.0,
            trigger_pattern="liquidity_sweep", threshold=40.0,
        )

    def test_it_ignores_the_signals_direction_entirely(self):
        """Unlike the other two. This gate is about the pattern being unsafe,
        not about which way the signal points -- a trend-aligned bounce is
        refused just the same."""
        blocked_with = extreme_trend_blocks(
            htf_bias="bullish", h4_bias="bullish", adx=45.0,
            trigger_pattern="bounce", threshold=40.0,
        )

        assert blocked_with

    def test_a_breakout_pattern_is_not_touched(self):
        assert extreme_trend_blocks(
            htf_bias="bullish", h4_bias="bullish", adx=45.0,
            trigger_pattern="breakout", threshold=40.0,
        ) is None

    def test_it_does_not_fire_when_the_two_timeframes_disagree(self):
        assert extreme_trend_blocks(
            htf_bias="bullish", h4_bias="bearish", adx=45.0,
            trigger_pattern="bounce", threshold=40.0,
        ) is None

    def test_it_does_not_fire_below_the_threshold(self):
        assert extreme_trend_blocks(
            htf_bias="bullish", h4_bias="bullish", adx=39.9,
            trigger_pattern="bounce", threshold=40.0,
        ) is None


_DIRECTIONS = ("BUY", "SELL")
_BIASES = ("bullish", "bearish", "neutral")
_PATTERNS = ("bounce", "liquidity_sweep", "breakout", "engulfing", None)
_SESSIONS = ("asian", "london", "ny", "off", None)
_ADX = (0.0, 24.9, 25.0, 39.9, 40.0, 60.0)


class TestTheExtractionChangedNothing:
    """The original inline expressions, copied from `test_signal_generate.py`
    as they stood before the extraction, evaluated against the extracted
    functions over every combination of their inputs.

    This is the whole safety argument for the move. A refactor of a rule that
    decides whether a trade happens is only safe if "same behaviour" is a fact
    rather than a reading, and these three rules have few enough inputs that
    the fact is cheap: 150 combinations for the Asian gate, 720 for each of the
    other two.
    """

    def test_the_asian_gate_matches_the_original_expression(self):
        for session, direction, htf_bias, pattern in itertools.product(
            _SESSIONS, _DIRECTIONS, _BIASES, _PATTERNS
        ):
            candidate = {"direction": direction, "trigger_pattern": pattern}
            # --- original ---
            original = False
            if session == "asian" and htf_bias != "neutral":
                _asian_counter = (
                    (candidate["direction"] == "BUY" and htf_bias == "bearish")
                    or (candidate["direction"] == "SELL" and htf_bias == "bullish")
                )
                if _asian_counter and candidate.get("trigger_pattern") != "liquidity_sweep":
                    original = True
            # --- extracted ---
            extracted = asian_counter_bias_blocks(
                session=session, direction=direction, htf_bias=htf_bias,
                trigger_pattern=pattern,
            ) is not None

            assert extracted == original, (session, direction, htf_bias, pattern)

    def test_the_dual_bias_gate_matches_the_original_expression(self):
        for direction, htf_bias, h4_bias, pattern, adx in itertools.product(
            _DIRECTIONS, _BIASES, _BIASES, _PATTERNS, _ADX
        ):
            candidate = {"direction": direction, "trigger_pattern": pattern}
            _db_threshold = 25.0
            # --- original ---
            original = False
            _dual_bias_trending = (
                htf_bias != "neutral"
                and h4_bias == htf_bias
                and adx >= _db_threshold
            )
            if _dual_bias_trending:
                _is_counter = (
                    (candidate["direction"] == "BUY" and htf_bias == "bearish")
                    or (candidate["direction"] == "SELL" and htf_bias == "bullish")
                )
                if _is_counter and candidate.get("trigger_pattern") != "liquidity_sweep":
                    original = True
            # --- extracted ---
            extracted = dual_bias_blocks(
                direction=direction, htf_bias=htf_bias, h4_bias=h4_bias, adx=adx,
                trigger_pattern=pattern, threshold=_db_threshold,
            ) is not None

            assert extracted == original, (direction, htf_bias, h4_bias, pattern, adx)

    def test_the_extreme_trend_gate_matches_the_original_expression(self):
        for direction, htf_bias, h4_bias, pattern, adx in itertools.product(
            _DIRECTIONS, _BIASES, _BIASES, _PATTERNS, _ADX
        ):
            candidate = {"direction": direction, "trigger_pattern": pattern}
            _extreme_adx = 40.0
            # --- original ---
            _extreme_trend = (
                htf_bias != "neutral" and h4_bias == htf_bias and adx >= _extreme_adx
            )
            original = bool(
                _extreme_trend
                and candidate.get("trigger_pattern") in ("bounce", "liquidity_sweep")
            )
            # --- extracted ---
            extracted = extreme_trend_blocks(
                htf_bias=htf_bias, h4_bias=h4_bias, adx=adx,
                trigger_pattern=pattern, threshold=_extreme_adx,
            ) is not None

            assert extracted == original, (htf_bias, h4_bias, pattern, adx)
