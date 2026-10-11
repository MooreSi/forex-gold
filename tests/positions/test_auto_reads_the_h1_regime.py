"""docs/todo/014, wired in behind a setting (owner 2026-10-11: "wire in and
make it a setting").

Auto (a source whose strategy is "auto") picks its template from the market
regime. Until now that regime came from 30 M5 bars in the Dynamic Profit
Management cache, which is EMPTY on this account (dpm_enabled = 0), so every
Auto pick was the "ranging" cell; and that classifier called ~60% of both a
choppy and a trending day "trending" anyway.

With `auto_template_h1_regime_enabled` on, Auto reads closed H1 bars from the
bridge and classifies them by efficiency ratio (services/market/h1_regime,
6 hours, trend at ER >= 0.4: the setting the 2026-10-06 study reported by):
trend -> "trending", range -> "ranging". Too few bars, or no bridge, reads as
"ranging", the same defensive default as before. Off (the default), nothing
changes: the M5 classifier as before, and no bridge call.

The template map itself is unchanged; the study found no cell worth refitting.
No channel is switched to Auto by this work.

Bridges are stubs serving canned candles. Nothing here reaches a broker.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from backend.src.services.positions import core_auto_template as auto

ON = {"auto_template_h1_regime_enabled": 1}
OFF = {"auto_template_h1_regime_enabled": 0}


def _bars(closes, start=4100.0):
    out, o = [], start
    for c in closes:
        out.append({"open": o, "high": max(o, c) + 1, "low": min(o, c) - 1, "close": c})
        o = c
    return out


RALLY = _bars([4105, 4110, 4116, 4120, 4127, 4133])
ZIGZAG = _bars([4108, 4099, 4109, 4098, 4107, 4101])
FORMING_DROP = {"open": 4133, "high": 4134, "low": 4060, "close": 4061}


class _Bridge:
    def __init__(self, bars=None, fail=False):
        self.bars, self.fail, self.asked = bars or [], fail, []

    async def get_candles(self, tf, n):
        self.asked.append((tf, n))
        if self.fail:
            raise RuntimeError("bridge down")
        return list(self.bars)


def _regime(rs, bridge, dpm=None):
    return asyncio.run(auto.live_regime(bridge, dpm, rs))


def test_off_uses_the_m5_classifier_and_asks_the_bridge_nothing():
    bridge = _Bridge(RALLY + [FORMING_DROP])
    assert _regime(OFF, bridge, dpm=[]) == auto.regime_from_candles([])
    assert bridge.asked == []


def test_on_a_steady_h1_rally_reads_trending():
    assert _regime(ON, _Bridge(RALLY + [FORMING_DROP])) == "trending"


def test_on_a_zigzag_reads_ranging():
    assert _regime(ON, _Bridge(ZIGZAG + [FORMING_DROP])) == "ranging"


def test_the_forming_bar_is_not_read():
    """The newest bar from get_candles is still forming. Here it is a crash
    that would wreck the rally's efficiency ratio if it were counted."""
    bridge = _Bridge(RALLY + [FORMING_DROP])
    assert _regime(ON, bridge) == "trending"
    assert bridge.asked == [("H1", auto.H1_REGIME_HOURS + 1)]


def test_an_empty_m5_cache_no_longer_forces_ranging():
    assert _regime(ON, _Bridge(RALLY + [FORMING_DROP]), dpm=[]) == "trending"


def test_too_few_bars_reads_ranging():
    assert _regime(ON, _Bridge(RALLY[:3])) == "ranging"


def test_a_failed_read_or_no_bridge_reads_ranging():
    assert _regime(ON, _Bridge(fail=True)) == "ranging"
    assert _regime(ON, None) == "ranging"


def test_every_auto_regime_site_uses_it():
    """The background cycle, the signal-time pick and the AI review must
    agree about the regime, so none of them may call the M5 classifier
    directly any more."""
    src = Path(auto.__file__).parents[1]
    for rel in ("signals/resolution.py", "channels/strategy_ai.py"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "live_regime(" in text, rel
        assert "regime_from_candles(" not in text, rel
    cycle = Path(auto.__file__).read_text(encoding="utf-8")
    body = cycle.split("async def run_auto_template_cycle", 1)[1]
    assert "live_regime(" in body and "regime_from_candles(" not in body
