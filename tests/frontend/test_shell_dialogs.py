"""The three shell dialogs are built with the page.

**Why this exists.** `frontend/app/__init__.py` sits 11 lines under the 800-line
ceiling, and the Power / Pause / Resume dialogs are the next seam to come out of
it (~165 lines). `test_main_page_renders.py` proves the page builds, but a
dialog that stopped being constructed raises nothing and renders nothing — the
page would still be green while the header's Power button opened an empty
overlay. Splitting untested code is what `docs/system/rules/70-file-organisation.md`
forbids, so this is the test that makes the move safe.

**What it does not cover.** That the buttons inside them do anything. `_do_pause`,
`_do_resume` and `_do_confirm_resume` write `trade_pause_until` and reset the
circuit breaker; none of that is exercised here.
"""
from __future__ import annotations

import pytest

# One line of fixed copy from each dialog. Not styling and not data.
DIALOG_LANDMARKS = [
    "Power Options",        # restart / stop
    "Pause Trading",        # the governor's trade_pause_until halt
    "Re-enable trading?",   # clears whichever halt is actually active
]


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", DIALOG_LANDMARKS)
async def test_the_shell_dialog_is_built(user, caption):
    await user.open("/")
    await user.should_see(caption)
