"""The 30m trigger reads CLOSED bars only (2026-10-01).

`shift_of_structure` fires on "the close, never the wick" -- but `gather`
handed it the bridge's series with the forming bar last, and a forming bar's
"close" is just the current price. An intrabar spike through the last swing
high was a trigger, and Auto could buy it, while the bar went on to close
back under the level. `patterns.confirmation` has always skipped that bar
for the same reason; the trigger now gets the same series minus it.
"""
import pytest

from backend.src.services.setforget import analysis, trigger

from ._candles import series

# A swing high at 110 (index 2), then lower bars, then the forming bar
# spiking to a "close" of 115 through it.
CLOSED = [100, 105, 110, 104, 102, 101, 103]
FORMING = 115


class _Engine:
    def __init__(self, m30):
        self.m30 = m30

    async def get_candles(self, timeframe, count=200):
        return self.m30 if timeframe == "M30" else []


def test_the_forming_bar_alone_would_have_been_a_shift():
    """The control: without the fix, this series IS a trigger."""
    fired = trigger.evaluate(series(CLOSED + [FORMING]), "BUY")
    assert fired and fired["kind"] == "shift_of_structure"


def test_nothing_fired_on_the_closed_bars():
    assert trigger.evaluate(series(CLOSED), "BUY") is None


@pytest.mark.asyncio
async def test_gather_hands_the_trigger_only_the_closed_bars():
    m30 = series(CLOSED + [FORMING])
    ev = await analysis.gather(_Engine(m30))
    assert ev["trigger_candles"] == m30[:-1]
    assert trigger.evaluate(ev["trigger_candles"], "BUY") is None
