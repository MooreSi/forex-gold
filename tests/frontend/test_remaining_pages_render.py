"""Landmarks for the pages still queued for a split.

Each of these renders during the dashboard render -- `test_panel` builds the
breakout and reversal panels inside itself, and `ai_trade_analysis` is reached
through the history page -- so pinning them needs no new harness, only the
shared `user` fixture.

Written BEFORE any of these files were split, so each split has something that
can tell a working page from a broken one. Every string below was verified to
be on the rendered page and to appear in exactly one of these six modules, so a
failure names the page that broke.

As with the history page: these assert the sections are BUILT, not that the
numbers in them are right.
"""
from __future__ import annotations

import pytest

# page -> the landmarks it alone puts on screen.
#
# Every entry below was validated the hard way: the page's render() was stubbed
# to return immediately and the suite re-run, and only strings that actually
# went red survived into this table. Two earlier candidates did not, and the
# reasons are worth keeping:
#
#   "Breakout Engine"     unique among these six files but ALSO rendered by
#                         frontend/pages/trading/_schedule.py, so it stayed on
#                         screen with breakout_panel.render() stubbed out.
#                         Uniqueness is now checked across all of frontend/.
#   "Learned Parameters"  and "Bounce" stayed findable by should_see with
#   / "Bounce"            test_panel stubbed, so neither can fail and neither
#                         is here. test_panel therefore had ONE validated
#                         landmark rather than a padded two -- a landmark that
#                         cannot go red is worse than not having it.
#
#   test_panel            REMOVED 2026-09-02. Its landmark was
#                         "TG LEARNING OFF", which lived in the Bounce panel;
#                         Bounce was removed on the owner's instruction and
#                         the module is now a pure tab host. Its only remaining
#                         text is the tab labels "Breakout" and
#                         "Reversal Engine", and BOTH appear elsewhere in
#                         frontend/ (_schedule.py, _signals_card.py,
#                         reversal_panel), so neither can fail if this page
#                         stops rendering. Rather than pad the table with a
#                         landmark that cannot go red -- the exact thing the
#                         note above rejects -- the page has no entry. Its two
#                         panels are covered by breakout_panel and
#                         reversal_panel above.
PAGE_LANDMARKS = {
    "ai_trade_analysis": ("AI Trade Analysis", "XAUUSD \u00b7 Per-channel signal quality"),
    "breakout_panel":    ("Engine Parameters", "M5 candle gate + 3s velocity monitor"),
    "chart":             ("RSI 14", "FVG:"),
    "reversal_panel":    ("Active Candidate Levels", "Learn From Pro Signals"),
    "telegram":          ("Telegram Authentication", "Step 1: Send login code"),
}

CASES = [(page, text) for page, texts in sorted(PAGE_LANDMARKS.items()) for text in texts]


@pytest.mark.asyncio
@pytest.mark.parametrize("page,landmark", CASES, ids=lambda v: v.replace(" ", "-"))
async def test_the_page_renders(user, page, landmark):
    await user.open("/")
    await user.should_see(landmark)


@pytest.mark.asyncio
async def test_the_landmark_check_can_actually_fail(user):
    """Negative control: absence has to be detectable."""
    await user.open("/")
    await user.should_not_see("__not_a_page_landmark__")
    with pytest.raises(AssertionError):
        await user.should_see("__not_a_page_landmark__")
