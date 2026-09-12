"""The Bounce engine's three counter-trend gates, as functions.

Lifted out of `TestSignalEngine.generate` (`test_signal_generate.py` sections
7a2, 7b and 7c), where they were inline boolean expressions inside a method
that cannot be called without candles, a bridge and a database -- so nothing
tested them. Each returns the reason it refused, or None to allow, which is the
shape the rest of the risk surface already uses
(`risk/governor.htf_bias_blocks`, `risk/capability_gates`).

**Behaviour is unchanged, and that is checked rather than asserted.**
`tests/test_signal/test_generate_gates.py` evaluates the original expressions,
copied verbatim from the source before this move, against these functions over
every combination of their inputs.

The three are close enough to be confused and differ in ways that matter:

| | fires on | direction | a liquidity sweep |
|---|---|---|---|
| `extreme_trend_blocks` | H1+H4 agree, ADX >= tunable | ignored | **refused too** |
| `dual_bias_blocks` | H1+H4 agree, ADX >= tunable | counter only | exempt |
| `asian_counter_bias_blocks` | the Asian session | counter only | exempt |

The sweep asymmetry is deliberate: a sweep's premise is that a level holds
against the crowd, and the extreme-trend gate exists precisely because levels
stop holding in a persistent trend.

**One open question sits on the third of these.** It refuses counter-bias
signals in the Asian session, and it has said so since before anything was
measured. The Reversal Engine's own numbers for the same hours say the
opposite (`capability_gates.asian_bias_exempt`, and the risk domain README).
Nothing here has been changed on that account -- the two engines trade
different setups and it is possible both are right. It is the owner's call:
`docs/simon-handover/033`.
"""
from __future__ import annotations

from typing import Optional


def _is_counter_bias(direction: str, htf_bias: str) -> bool:
    """Does this signal point against the higher-timeframe bias?

    A neutral bias has no counter side, which is why every caller checks the
    bias is not neutral before asking.
    """
    return ((direction == "BUY" and htf_bias == "bearish")
            or (direction == "SELL" and htf_bias == "bullish"))


def extreme_trend_blocks(*, htf_bias: str, h4_bias: str, adx: float,
                         trigger_pattern: Optional[str],
                         threshold: float) -> Optional[str]:
    """Mean-reversion patterns are unsafe while a trend is persistent.

    Direction-blind, unlike the other two: a bounce aligned with the trend is
    refused just the same, because the objection is to the pattern's premise
    (that the level holds) and not to which way the signal points.
    """
    extreme = htf_bias != "neutral" and h4_bias == htf_bias and adx >= threshold
    if not extreme or trigger_pattern not in ("bounce", "liquidity_sweep"):
        return None
    return (
        f"Extreme trend block: H1+H4 both {htf_bias}, ADX {adx:.0f} >= {threshold:.0f} "
        f"— {trigger_pattern} pattern unsafe (levels don't hold in persistent trends)"
    )


def dual_bias_blocks(*, direction: str, htf_bias: str, h4_bias: str, adx: float,
                     trigger_pattern: Optional[str],
                     threshold: float) -> Optional[str]:
    """No counter-trend entry while H1 and H4 agree and ADX says they mean it."""
    trending = htf_bias != "neutral" and h4_bias == htf_bias and adx >= threshold
    if not trending:
        return None
    if not _is_counter_bias(direction, htf_bias) or trigger_pattern == "liquidity_sweep":
        return None
    return (
        f"Dual-bias block: H1+H4 both {htf_bias}, ADX {adx:.0f} ≥ {threshold:.0f} "
        f"— counter-trend {direction} blocked"
    )


def asian_counter_bias_blocks(*, session: Optional[str], direction: str,
                              htf_bias: str,
                              trigger_pattern: Optional[str]) -> Optional[str]:
    """Trend-aligned signals only, between 00:00 and 07:00 UTC.

    See the module docstring: this is the rule the Reversal Engine's data
    disagrees with, and the disagreement is an open question for the owner
    rather than something to resolve from here.
    """
    # The neutral check is redundant -- `_is_counter_bias` is already False for
    # a neutral bias -- and is kept because the original carried it and this
    # move changes nothing. Worth knowing if you mutation-test this file: a
    # mutant that deletes it SURVIVES, and that is an equivalent mutant rather
    # than a hole in the tests.
    if session != "asian" or htf_bias == "neutral":
        return None
    if not _is_counter_bias(direction, htf_bias) or trigger_pattern == "liquidity_sweep":
        return None
    return (
        f"Asian counter-bias block: HTF {htf_bias}, signal {direction} — "
        "only trend-aligned signals in Asian session"
    )
