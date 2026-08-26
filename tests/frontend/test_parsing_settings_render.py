"""The Parsing tab's settings block has to actually be on the page.

Written because it was not. The upstream merge (1e383fe) brought in a
rewritten, category-grouped Parsing Settings block and the `render()` call
site that goes with it, but resolved the conflict by leaving
`_render_parsing_settings_section` an empty stub and parking the whole body
in `_render_logic_keywords_section`, which nothing calls. Every switch on
the tab -- Auto-Execution, Immediate Market Buy/Sell, TP/SL in Second
Message, Reverse/Mirror Copy, the lexicon boxes -- silently vanished from
the UI while remaining fully wired in the backend, so a setting could not be
turned on and signals it governs were missed.

The existing telegram landmarks in test_remaining_pages_render.py pinned only
the auth wizard, which is why nothing went red. These pin the settings.

Renders only: no MT5 call, no order, no money. The strings below were each
checked to appear in exactly one module under frontend/, so a failure names
the block that broke rather than the page.
"""
from __future__ import annotations

import pytest

# Landmark -> what its absence means. Each is unique across frontend/.
PARSING_LANDMARKS = [
    "Parsing Settings",             # the section header itself
    "Immediate Market Buy/Sell",    # EXECUTION toggle grid
    "Reverse / Mirror Copy",        # EXECUTION toggle grid, upstream-only row
    "Queue Closed Market Limits",   # MARKET GUARD toggle grid
    "Second-message match window:",  # the numeric inputs below the grids
    "Fallback SL distance:",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("landmark", PARSING_LANDMARKS, ids=lambda v: v.replace(" ", "-"))
async def test_the_parsing_settings_block_is_on_the_page(user, landmark):
    await user.open("/")
    await user.should_see(landmark)


@pytest.mark.asyncio
async def test_every_parsing_toggle_key_reaches_the_page(user):
    """The grid is data-driven, so pin the data as well as the render: every
    key in _PARSING_CATEGORIES must put its label on screen. A row silently
    dropped from the table would otherwise pass the landmarks above."""
    from frontend.pages.telegram._keywords import _PARSING_CATEGORIES

    await user.open("/")
    labels = [label for _, _, toggles in _PARSING_CATEGORIES
              for _, label, _, _ in toggles]
    assert len(labels) >= 12, f"expected the full toggle set, got {len(labels)}"
    for label in labels:
        await user.should_see(label)
