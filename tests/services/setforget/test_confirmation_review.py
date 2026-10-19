"""Regression cases from the 2026-10-19 strategy review. Pure OHLC only;
the fake reader exposes no order method and cannot contact a broker.
"""
import asyncio

import pytest

from backend.src.services.setforget import analysis, aoi, prompt
from backend.src.services.dpm import engine as market_clock
from ._candles import candle
from .test_analysis import _evidence, _zone


def engulfing_bars():
    return [candle(i * 1800, 2000, 2000.4, 1999.6, 2000.1)
            for i in range(6)] + [
        candle(10800, 2000, 2000.5, 1997, 1997.5),
        candle(12600, 1997.2, 2001.5, 1997, 2001),
    ]


class Reader:
    def __init__(self, bars):
        self.bars = bars

    async def get_candles(self, timeframe, count=200):
        return self.bars if timeframe == "M30" else []


def test_gather_and_engulfing_agree_on_the_latest_closed_bar():
    bars = engulfing_bars()
    reader = Reader(bars + [candle(14400, 2001, 2002, 2000, 2001.1)])
    ev = asyncio.run(analysis.gather(reader))
    ev.update(_evidence(price=2001, zones=[_zone("demand", 1995, 2002),
                                          _zone("supply", 2040, 2050)]))
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["stage"] == "triggered"
    assert candidate["trigger"]["ts"] == 12600


def test_a_forming_engulfing_cannot_trigger_through_gather():
    ev = asyncio.run(analysis.gather(Reader(engulfing_bars())))
    ev.update(_evidence(price=2001, zones=[_zone("demand", 1995, 2002)]))
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["stage"] == "waiting"


def reaction_evidence(direction="BUY", **over):
    # The last bar rejects the demand with its WICK and closes beyond the
    # 0.5 x H4 ATR arrival band. Requiring arrival at that close misses it.
    bars = engulfing_bars()
    bars[-1] = candle(12600, 1997.2, 2005, 1996, 2004)
    ev = _evidence(price=2004, atr=4, confirmation=None,
                   zones=[_zone("demand", 1995, 2000),
                          _zone("supply", 2040, 2050)], trigger_candles=bars)
    if direction == "SELL":
        ev["price"] = 4000 - ev["price"]
        ev["zones"] = [_zone("supply", 2000, 2005), _zone("demand", 1950, 1960)]
        ev["trigger_candles"] = [dict(c, open=4000-c["open"],
            high=4000-c["low"], low=4000-c["high"], close=4000-c["close"])
            for c in bars]
    ev.update(over)
    return ev


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_a_recent_zone_rejection_can_confirm_just_outside_the_zone(direction):
    candidate, why = analysis.propose(reaction_evidence(direction), direction=direction)
    assert candidate is not None, why
    assert candidate["stage"] == "triggered"
    assert candidate["entry"] == reaction_evidence(direction)["price"]
    assert candidate["trigger"]["zone_touch"]["ts"] == 12600
    assert candidate["target_basis"] == "opposing_zone"


def test_a_trigger_elsewhere_does_not_claim_a_zone_visit():
    ev = reaction_evidence()
    ev["trigger_candles"] = [dict(c, open=c["open"]+50, high=c["high"]+50,
                                low=c["low"]+50, close=c["close"]+50)
                              for c in ev["trigger_candles"]]
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["stage"] == "armed"


def test_a_reaction_does_not_license_chasing_a_move():
    candidate, _ = analysis.propose(reaction_evidence(price=2020), direction="BUY")
    assert candidate["stage"] == "armed"


def test_a_close_through_the_distal_stop_invalidates_a_visit():
    ev = reaction_evidence()
    # The later engulfing no longer touches the AOI; a failed original visit
    # must not be recycled to justify it.
    ev["trigger_candles"][-1] = candle(12600, 1997.2, 2005, 1992, 1993)
    ev["trigger_candles"] += [candle(14400, 2004, 2005, 2003, 2003.5),
                               candle(16200, 2003.4, 2006, 2003, 2005.5)]
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["stage"] == "armed"


def test_a_reaction_stop_includes_the_wick_that_visited_the_zone():
    ev = reaction_evidence()
    ev["trigger_candles"][-1]["low"] = 1993
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["stop_loss"] == pytest.approx(1992)


def test_overlapping_levels_still_combine_inside_the_width_rail():
    merged = aoi.merge([_zone("demand", 1950, 1960), _zone("demand", 1955, 1970)],
                       max_width=30)
    assert len(merged) == 1
    assert (merged[0]["low"], merged[0]["high"], merged[0]["touches"]) == (1950, 1970, 2)


def test_an_old_zone_visit_does_not_rearm_a_fresh_engulfing():
    ev = reaction_evidence()
    ev["trigger_candles"] += [candle(14400+i*1800, 2004, 2005, 2003, 2004.1)
                              for i in range(10)]
    ev["trigger_candles"] += [candle(32400, 2004, 2005, 2003, 2003.5),
                               candle(34200, 2003.4, 2006, 2003, 2005.5)]
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["stage"] == "armed"


def test_a_triggered_stop_uses_the_entry_reaction_not_an_unrelated_h4_tail():
    ev = reaction_evidence(confirmation={"direction": "bullish", "kind": "pin_bar",
                                        "low": 1950, "high": 2010})
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["stop_loss"] == pytest.approx(1994)


def test_combining_timeframes_cannot_chain_a_level_past_the_width_rail():
    zones = [_zone("demand", 1950, 1970), _zone("demand", 1965, 1985),
             _zone("demand", 1980, 2000)]
    merged = aoi.merge(zones, max_width=2000 * aoi.MAX_ZONE_WIDTH_PCT)
    assert len(merged) == 3
    assert len(aoi.within_width(merged, 2000)) == 3


def test_ai_is_told_the_trigger_and_existing_three_r_fallback_rule():
    ev = reaction_evidence(zones=[_zone("demand", 1995, 2000)])
    candidate, _ = analysis.propose(ev, direction="BUY")
    rendered = prompt.render(ev, candidate)
    assert "Stage: triggered" in rendered
    assert "30m trigger: engulfing" in rendered
    assert "Zone visit:" in rendered
    assert "3R fallback" in rendered
    assert "3R fallback" in prompt.SYSTEM


def test_ai_cannot_adjust_a_stop_inside_the_measured_zone():
    ev = reaction_evidence()
    candidate, _ = analysis.propose(ev, direction="BUY")
    revised, rejected = analysis._review_levels(
        {"verdict": "adjust", "entry": 2004, "stop_loss": 1998, "take_profit": 2040},
        candidate, 2004, 4)
    assert revised is None
    assert any("structural stop" in reason for reason in rejected)


def test_ai_cannot_move_a_target_through_the_nearest_opposing_zone():
    ev = reaction_evidence()
    candidate, _ = analysis.propose(ev, direction="BUY")
    revised, rejected = analysis._review_levels(
        {"verdict": "adjust", "entry": 2004, "stop_loss": 1994, "take_profit": 2080},
        candidate, 2004, 4)
    assert revised is None
    assert any("opposing zone" in reason for reason in rejected)


def test_ai_cannot_change_a_market_entry_to_manufacture_a_better_ratio():
    candidate, _ = analysis.propose(reaction_evidence(), direction="BUY")
    revised, rejected = analysis._review_levels(
        {"verdict": "adjust", "entry": 2000, "stop_loss": 1994, "take_profit": 2040},
        candidate, 2004, 4)
    assert revised is None
    assert any("market entry" in reason for reason in rejected)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_ai_prices_must_be_finite(bad):
    candidate, _ = analysis.propose(reaction_evidence(), direction="BUY")
    revised, rejected = analysis._review_levels(
        {"verdict": "adjust", "entry": 2004, "stop_loss": 1994, "take_profit": bad},
        candidate, 2004, 4)
    assert revised is None
    assert any("finite" in reason for reason in rejected)


def test_an_opposing_zone_containing_entry_cannot_be_skipped_for_a_distant_target():
    ev = reaction_evidence(price=2001, zones=[_zone("demand", 1995, 2000),
                                 _zone("supply", 2000.5, 2010),
                                 _zone("supply", 2040, 2050)])
    candidate, _ = analysis.propose(ev, direction="BUY")
    assert candidate["target_zone"]["low"] == 2000.5
    assert candidate["take_profit"] <= candidate["entry"]


def test_a_closed_market_does_not_repeatedly_present_fridays_signal_for_ai_review(monkeypatch):
    monkeypatch.setattr(market_clock, "is_weekly_market_closed", lambda: True)
    ev = asyncio.run(analysis.gather(Reader(engulfing_bars())))
    ev.update(_evidence(price=2001, zones=[_zone("demand", 1995, 2002)]))
    candidate, why, other = analysis.scan(ev)
    assert candidate is None and other is None
    assert "market is closed" in why.lower()
