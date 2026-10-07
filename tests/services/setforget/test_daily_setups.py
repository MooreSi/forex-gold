"""Set & Forget finds setups most days (owner, 2026-10-07).

The owner reported about two setups a month where Alex G's students trade it
daily. Replaying the page's own functions at every 30m close for a year
(Oct 2025 - Oct 2026, no AI, one trade at a time, $0.30 a trade):

  as shipped                        placeable on  71 of ~236 days, -0.14R a trade
  4H levels + arrival 0.5 + 3R      placeable on 156 of ~236 days, +0.20R a trade
                                    (both halves positive: +0.03 / +0.38)

Three changes, each measured on its own (docs/system/domains/trading/
020-set-and-forget.md, "Daily setups"):

  * Levels are also marked on the 4H. On Daily/Weekly alone gold spent most of
    the year between two distant levels.
  * "At the zone" is half a 4H ATR, not a whole one. A whole one ($30-40)
    called a BUY 37 points above its demand "at" it; the AI declined those as
    mid-range on 5 Oct, every 15 minutes.
  * No opposing level (price at or near an all-time high): target 3R, the
    guide's preferred ratio, instead of refusing every long.
"""
import pytest

from backend.src.services.setforget import analysis

from tests.services.setforget.test_analysis import _Engine, _evidence, _zone, zigzag


def test_the_defaults_are_the_measured_ones():
    assert analysis.ARRIVAL_ATR == 0.5
    assert analysis.TARGET_FALLBACK_R == 3.0
    assert analysis.ZONES_FROM_4H is True


@pytest.mark.asyncio
async def test_the_4h_marks_a_level_of_its_own():
    engine = _Engine({
        "D1": zigzag([(500.0, 0), (600.0, 8), (400.0, 8), (600.0, 8),
                      (400.0, 8), (600.0, 8), (400.0, 8), (520.0, 6)]),
        "H4": zigzag([(500.0, 0), (520.0, 8), (500.0, 8), (520.0, 8),
                      (500.0, 8), (520.0, 8), (510.0, 5)]),
    })
    ev = await analysis.gather(engine)
    assert any(515.0 <= z["low"] <= 525.0 or 515.0 <= z["high"] <= 525.0
               for z in ev["zones"]), ev["zones"]


def test_a_whole_atr_away_is_not_at_the_zone():
    # ATR 6: price 5 above the demand top. Under the old whole-ATR rule this
    # was "waiting" at the zone; at half an ATR (3) it is still on its way.
    c, _ = analysis.propose(_evidence(price=1990.0), direction="BUY")
    assert c["stage"] == "armed"


def test_inside_the_zone_is_at_it():
    c, _ = analysis.propose(_evidence(price=1983.0), direction="BUY")
    assert c["stage"] in ("waiting", "triggered")


def test_with_no_level_above_a_long_targets_three_r():
    ev = _evidence(zones=[_zone("demand", 1975.0, 1985.0)], price=1983.0)
    c, why = analysis.propose(ev, direction="BUY")
    assert c is not None, why
    risk = c["entry"] - c["stop_loss"]
    assert c["take_profit"] == pytest.approx(c["entry"] + 3.0 * risk)
    assert c["target_zone"] is None
