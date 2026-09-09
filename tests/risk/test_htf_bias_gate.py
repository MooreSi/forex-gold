"""Trades that disagree with the higher-timeframe bias can be refused.

docs/todo/reversal-engine/080 and /090, from the 2026-09-08 post-mortem.

**The evidence.** Across every executed Reversal Engine signal on record:

| | n | win % | net |
|---|---|---|---|
| with the bias | 369 | 61.8 | **+$101.41** |
| against it | 201 | 56.2 | -$1,210.98 |
| bias neutral | 184 | 57.1 | -$1,234.06 |

Trading WITH the higher-timeframe bias is the only profitable group in the
whole history. On 2026-09-08 gold fell 4438 -> 4391 and the system bought it
46 times for -$997.

**Two holes, and the second is the one that mattered.** The Reversal Engine
had a bias check that a high `level_score` bypassed -- and `level_score` does
not rank outcomes (its 0.9 band is the biggest losing band on record). But the
day's losses were TEMPLATE trades, and `resolution.py` skips
`check_pre_trade_filters` entirely for templates:

    if strategy not in _self_level_strategies and not _is_template:

So no pre-trade filter ran on them at all. A gate added only inside
`check_pre_trade_filters` would not have stopped a single one of that day's
trades. This one is checked for every strategy including templates.

**Default OFF.** Per docs/system/rules/60-adding-a-tunable, a new setting's
default must be byte-identical to the behaviour it replaces. Nothing changes
until the owner turns it on, and it has never been demoed.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import governor


def _rs(on=1):
    return {"htf_bias_gate_enabled": on}


class TestTheToggleDecidesWhetherItRunsAtAll:
    @pytest.mark.parametrize("direction,bias", [
        ("BUY", "bearish"), ("SELL", "bullish"),
    ])
    def test_off_by_default_nothing_is_blocked(self, direction, bias):
        """A setting absent from the row must behave as it did before the
        setting existed."""
        assert governor.htf_bias_blocks(direction, bias, {}) is None

    @pytest.mark.parametrize("direction,bias", [
        ("BUY", "bearish"), ("SELL", "bullish"),
    ])
    def test_explicitly_off_is_the_same(self, direction, bias):
        assert governor.htf_bias_blocks(direction, bias, _rs(0)) is None


class TestWhatItBlocksWhenOn:
    def test_a_buy_against_a_bearish_bias_is_refused(self):
        """2026-09-08 in one assertion."""
        reason = governor.htf_bias_blocks("BUY", "bearish", _rs())

        assert reason is not None
        assert "bearish" in reason.lower()

    def test_a_sell_against_a_bullish_bias_is_refused(self):
        assert governor.htf_bias_blocks("SELL", "bullish", _rs()) is not None

    def test_the_reason_names_what_caused_it(self):
        """An operator seeing a refusal must be able to find the switch."""
        reason = governor.htf_bias_blocks("BUY", "bearish", _rs())

        assert "bias" in reason.lower()


class TestWhatItMustNOTBlock:
    def test_a_buy_with_a_bullish_bias_proceeds(self):
        assert governor.htf_bias_blocks("BUY", "bullish", _rs()) is None

    def test_a_sell_with_a_bearish_bias_proceeds(self):
        assert governor.htf_bias_blocks("SELL", "bearish", _rs()) is None

    def test_a_neutral_bias_proceeds(self):
        """Neutral loses money too (-$1,234 over 184 trades), but "no clear
        trend" is not the same claim as "the trend is against you", and
        blocking it is a bigger change than was asked for. Recorded in 090
        rather than smuggled in here.

        Note: deleting the explicit `bias not in ("bullish", "bearish")` guard
        is an EQUIVALENT mutation — the final condition only fires for those
        two values anyway, so "neutral" falls through to None either way. The
        guard is kept because it states the intent, and this is recorded here
        rather than chased with a test that cannot exist.
        """
        assert governor.htf_bias_blocks("BUY", "neutral", _rs()) is None

    @pytest.mark.parametrize("unknown", ["", None, "unknown", "  "])
    def test_an_UNKNOWN_bias_proceeds(self, unknown):
        """The bias is unavailable when candles are missing or the bridge is
        down. A risk filter that blocks on missing DATA stops all trading the
        moment a feed hiccups -- it must fail open on not-knowing, and closed
        only on knowing the trend is against."""
        assert governor.htf_bias_blocks("BUY", unknown, _rs()) is None

    def test_case_and_whitespace_do_not_defeat_it(self):
        """Direction arrives upper-cased from some paths and not others."""
        assert governor.htf_bias_blocks(" buy ", " BEARISH ", _rs()) is not None


class TestItIsOneRuleForEverySource:
    """080 and 090 are the same rule on two routes. Two implementations that
    can disagree is how this class of bug starts -- so there is one function
    and both callers use it."""

    def test_the_reversal_engine_uses_this_function(self):
        import inspect
        from backend.src.services.reversal_engine import reversal_engine_live_execute as rle

        assert "htf_bias_blocks" in inspect.getsource(rle)

    def test_the_shared_open_path_uses_it_too(self):
        import inspect
        from backend.src.services.signals import resolution

        assert "htf_bias_blocks" in inspect.getsource(resolution)

    def test_the_open_path_checks_it_OUTSIDE_the_template_exemption(self):
        """The whole point. resolution.py skips check_pre_trade_filters for
        templates, and every trade that lost money on 2026-09-08 was a
        template. If this gate sits inside that exemption it protects nothing
        that actually needs protecting."""
        import inspect
        from backend.src.services.signals import resolution

        src = inspect.getsource(resolution)
        # The CALL, not the import line -- the import necessarily sits at the
        # top of the file and would satisfy a naive search regardless of where
        # the gate actually runs.
        gate_at = src.index("check_htf_bias(")
        exemption_at = src.index("and not _is_template")
        assert gate_at > exemption_at, (
            "the bias gate is inside the block that templates skip"
        )
        # And it must not be indented INSIDE that block either: same column as
        # the `if`, so it runs for every strategy.
        line = src[:gate_at].rsplit("\n", 1)[-1]
        assert len(line) - len(line.lstrip()) <= 4, (
            f"the gate is nested {len(line) - len(line.lstrip())} spaces deep"
        )
