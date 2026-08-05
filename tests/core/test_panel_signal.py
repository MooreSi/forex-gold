"""The EA panel's live signal dashboard (core_panel_signal).

This is a display feed -- nothing here sizes, places or blocks a trade -- so
what these tests protect is the two properties that would make the panel
actively misleading rather than merely wrong:

  * a data failure must render as "no data", never as the previous cycle's
    numbers or as a confident zero-confidence read;
  * the score must be a function of the criteria the panel is simultaneously
    showing as Y/N, so a user cannot see four Ys next to a D grade.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from forex_trader.core import core_panel_signal as ps


class _Bridge:
    """Minimal stand-in for mt5_bridge: whatever candles the test hands it."""

    def __init__(self, candles=None, tick=SimpleNamespace(bid=4000.0, ask=4000.2),
                 raise_on_candles=False):
        self._candles = candles or {}
        self._tick = tick
        self._raise = raise_on_candles

    async def get_candles(self, tf, n):
        if self._raise:
            raise RuntimeError("bridge down")
        return self._candles.get(tf, [])

    async def get_tick(self):
        return self._tick


def _flat(n, tf_secs, base=4000.0):
    """A featureless series: no gaps, no impulse, no swing structure. The
    point is that every ICT criterion should read N on it, so a test that
    scores it non-zero is testing the scoring, not the patterns."""
    return [{"ts": 1_700_000_000 + i * tf_secs, "open": base, "high": base,
             "low": base, "close": base, "tick_volume": 100} for i in range(n)]


def test_weights_sum_to_100():
    """The panel prints the score as "(60pts)" with no scale beside it, so it
    only reads as a percentage while the weights total 100."""
    assert sum(ps.WEIGHTS.values()) == 100


@pytest.mark.parametrize("points,grade", [
    (100, "A"), (75, "A"), (74, "B"), (50, "B"),
    (49, "C"), (30, "C"), (29, "D"), (0, "D"),
])
def test_grade_boundaries(points, grade):
    assert ps._grade(points) == grade


def test_killzone_windows_match_the_declared_table():
    """KILLZONES is duplicated in the EA (PanelSession) for the ticking
    countdown. This pins the Python half so a change here is a visible diff
    rather than a silent divergence."""
    for name, start, end in ps.KILLZONES:
        inside = datetime(2026, 1, 5, start, 30, tzinfo=timezone.utc)
        assert ps.in_killzone(inside) == (True, name)
    # 22:00 UTC falls in no window at all.
    assert ps.in_killzone(datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)) == (False, "")


def test_bridge_failure_returns_empty_payload_not_stale_numbers():
    p = asyncio.run(ps.build_payload(_Bridge(raise_on_candles=True)))
    assert p == ps.empty_payload("NO FEED")
    assert p["buy_conf"] == 0 and p["sell_conf"] == 0
    assert p["bias"] == "NEUTRAL"


def test_missing_candles_returns_empty_payload():
    """An empty candle list is the shape a not-yet-warm bridge returns; it
    must not fall through into the scoring path with len()==0 series."""
    p = asyncio.run(ps.build_payload(_Bridge(candles={})))
    assert p["scanner"] == "NO FEED"


def test_zero_price_tick_is_refused():
    b = _Bridge(candles={"M5": _flat(120, 300), "M15": _flat(120, 900),
                         "H1": _flat(50, 3600), "H4": _flat(12, 14400)},
                tick=SimpleNamespace(bid=0.0, ask=0.0))
    assert asyncio.run(ps.build_payload(b))["scanner"] == "NO PRICE"


def test_featureless_market_scores_only_the_criteria_it_shows():
    """On a flat series every pattern is absent, so the only points available
    are the time-of-day and mean-reversion ones -- and each must be matched by
    a Y in the criteria row the panel draws next to the score."""
    b = _Bridge(candles={"M5": _flat(120, 300), "M15": _flat(120, 900),
                         "H1": _flat(50, 3600), "H4": _flat(12, 14400)})
    p = asyncio.run(ps.build_payload(b))

    assert p["fvg"] is False
    assert p["sweep"] is False
    assert p["displacement"] is False
    assert p["order_block"] is False
    assert p["bias"] == "NEUTRAL"

    # A flat series puts price exactly on its own VWAP, so both sides read OK.
    expected = ps.WEIGHTS["vwap"]
    if p["killzone"]:
        expected += ps.WEIGHTS["killzone"]
    assert p["buy_conf"] == expected
    assert p["sell_conf"] == expected


def test_score_never_credits_bias_align_against_a_neutral_bias():
    """bias_align is the largest single weight. Crediting it on NEUTRAL would
    give every quiet market a free 25 points on both sides at once."""
    b = _Bridge(candles={"M5": _flat(120, 300), "M15": _flat(120, 900),
                         "H1": _flat(50, 3600), "H4": _flat(12, 14400)})
    p = asyncio.run(ps.build_payload(b))
    assert p["bias"] == "NEUTRAL"
    assert p["bias_align"] is False
    assert p["buy_conf"] < ps.WEIGHTS["bias_align"]


def test_vwap_falls_back_to_typical_price_when_the_feed_reports_no_volume():
    """Some XAUUSD feeds report zero tick volume. Returning None there would
    blank two criteria for a reason that has nothing to do with the market --
    and would silently disagree with the EA, which makes the same fallback."""
    candles = [{"ts": i, "open": 10.0, "high": 12.0, "low": 8.0,
                "close": 10.0, "tick_volume": 0} for i in range(5)]
    assert ps._vwap(candles) == pytest.approx(10.0)


def test_vwap_weights_by_volume_when_it_is_present():
    candles = [
        {"ts": 0, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "tick_volume": 1},
        {"ts": 1, "open": 20.0, "high": 20.0, "low": 20.0, "close": 20.0, "tick_volume": 3},
    ]
    assert ps._vwap(candles) == pytest.approx(17.5)


def test_displacement_needs_a_body_clearly_larger_than_recent_average():
    base = [{"ts": i, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2,
             "tick_volume": 10} for i in range(30)]
    assert ps._has_displacement(base) is False
    spiked = list(base)
    spiked[-1] = {"ts": 99, "open": 100.0, "high": 110.0, "low": 100.0,
                  "close": 109.0, "tick_volume": 10}
    assert ps._has_displacement(spiked) is True


def test_levels_are_empty_when_the_reversal_engine_is_not_running():
    """The LEVELS tab shows what the engine would actually trade. With no
    engine there is nothing to show -- and crucially no second opinion
    computed here to fill the gap."""
    assert ps._levels(4000.0) == []
