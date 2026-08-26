"""Every section of the Analysis (history) page really renders.

Written before `frontend/pages/history.py` was split into a package, so the
split has something that can tell it apart from a working page. Coverage of
this page was otherwise two pure helpers (`tests/ui/test_history_session_
attribution.py`) and the controller -- nothing that renders it.

The plan for the split
(`docs/todo/refactor/frontend/restructure/phase2-view-decomposition/030-history.md`)
says phase-1 pinned the displayed numbers in
`test_history_numbers_characterization.py` and that this task can lean on it.
That file does not exist. This does not replace it: these tests assert the
sections are BUILT, not that the win rate or profit factor is right. Pinning
the numbers is still outstanding.

`history.render(get_engine)` runs during the dashboard render, so the shared
`user` fixture reaches it without any special setup.
"""
from __future__ import annotations

import pytest

# One landmark per section, taken from the section that owns it. Captions
# rendered at page load, so they appear with an empty database.
#
# Deliberately NOT ui.table column labels ("Ticket", "Order Type"): those are
# element props rather than text, so should_see cannot match them and a test
# built on them fails for a reason that has nothing to do with the page. Nor
# the calendar's "By Signal Source", which only exists after a day is clicked.
SECTION_LANDMARKS = {
    "the tab strip":     "Trade History",
    "the trade table":   "Closed Trades",
    "the equity curve":  "Equity Curve",
    "the calendar":      "Refresh from MT5",
    "the heat map":      "Performance Heat Map",
    "the channels view": "Channel Scorecard",
    "the summary stats": "Profit Factor",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section,landmark", sorted(SECTION_LANDMARKS.items()), ids=lambda v: v.replace(" ", "-")
)
async def test_the_history_section_renders(user, section, landmark):
    await user.open("/")
    await user.should_see(landmark)


@pytest.mark.asyncio
async def test_the_section_check_can_actually_fail(user):
    """Negative control: absence has to be detectable, or the above is noise."""
    await user.open("/")
    await user.should_not_see("__not_a_history_caption__")
    with pytest.raises(AssertionError):
        await user.should_see("__not_a_history_caption__")
