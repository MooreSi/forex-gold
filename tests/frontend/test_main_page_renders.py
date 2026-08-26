"""The dashboard actually renders, and its header is really there.

**Why this exists.** `frontend/app/` holds the single `@ui.page("/")`, and
until now nothing rendered it. `test_pages_render.py` is by its own docstring
"an import-and-signature check rather than a full render", and
`test_app_boots.py` says out loud that on an unlicensed checkout it serves the
activation screen and the dashboard never renders. Between them, a header
rebuilt wrongly -- a badge wired to nothing, a panel that stopped being added
-- passed green. That blocked the app-shell split, because the only seam that
brings `__init__.py` under the 800-line ceiling is the header itself
(`docs/todo/refactor/frontend/restructure/phase2-view-decomposition/041-*.md`).

**How it renders without a licence or a broker.** `guard.enforce()` is called
by `run.py` before the server starts, not as route middleware, so rendering
the page in-process never reaches it. `_render/main_shim.py` is a NiceGUI main
file executed inside `user_simulation`'s reset context; it stubs the lifespan
hooks before `frontend.app` binds them, points every database namespace at a
temp file, and injects fakes for the engine and Telegram reader. The real
`backend.src.app.startup` opens the user's live trading database and rewrites
`bridge_credentials.json`, and the real engine opens an MT5 bridge connection.
Neither may happen from a test -- see the golden rules.

**What this does not cover.** It asserts the header is built, not that it
updates. `_refresh_header` runs on a 2-second `ui.timer` and is not exercised
here; a badge that renders correctly and then never refreshes still passes.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib

import pytest
import pytest_asyncio
from nicegui.testing.user_simulation import user_simulation

SHIM = pathlib.Path(__file__).parent / "_render" / "main_shim.py"

# Fixed text the header bar builds. Not styling and not data -- these are the
# panel captions, so they are present whether or not a broker ever answers.
HEADER_LANDMARKS = ["BID", "ASK", "MT5 BAL", "EQUITY"]


@pytest_asyncio.fixture
async def user(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREX_RENDER_TEST_DB", str(tmp_path / "render.db"))
    async with user_simulation(main_file=SHIM) as simulated:
        yield simulated


@pytest.mark.asyncio
async def test_the_dashboard_renders(user):
    """The whole page builds -- every tab panel included.

    NiceGUI turns an exception in a page function into a 500 page, so this
    fails loudly rather than serving something broken.
    """
    await user.open("/")
    await user.should_see("FOREX Trader")


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", HEADER_LANDMARKS)
async def test_the_header_bar_is_built(user, caption):
    """Each header panel is present.

    This is the check that makes extracting the header safe: a build_header()
    that forgets a panel, or is never called, fails here.
    """
    await user.open("/")
    await user.should_see(caption)


@pytest.mark.asyncio
async def test_the_header_check_can_actually_fail(user):
    """Negative control.

    A render assertion that passes no matter what the page contains would
    make every test above worthless -- docs/system/rules/40-testing.md names
    that failure directly. So assert the same mechanism reports absence.
    """
    await user.open("/")
    await user.should_not_see("__not_a_header_caption__")

    with pytest.raises(AssertionError):
        await user.should_see("__not_a_header_caption__")


@pytest.mark.asyncio
async def test_the_page_renders_without_touching_the_real_database(user, tmp_path):
    """The isolation this file depends on, asserted rather than assumed.

    If the shim ever stops redirecting the database, these tests would render
    against the live trading database and nothing would say so.
    """
    from backend.src.db import database as db

    await user.open("/")
    assert str(tmp_path) in str(db._DB_PATH), (
        f"render test is pointed at {db._DB_PATH}, not its temp database"
    )


@pytest.mark.asyncio
async def test_the_header_refresh_runs_without_error(user, caplog):
    """The 2-second refresh actually completes.

    Rendering proves the header was BUILT. This proves the callback behind it
    runs: _refresh_header awaits the engine and writes into the header
    widgets, and it catches its own exceptions and logs a warning rather than
    failing loudly, so without this assertion a refresh that dies every tick
    looks identical to one that works.

    That is not hypothetical -- with sync stubs on the fake engine this caught
    "object NoneType can't be used in 'await' expression" while all the render
    assertions above still passed.
    """
    with caplog.at_level(logging.WARNING, logger="frontend.app"):
        await user.open("/")
        await asyncio.sleep(2.5)   # the ui.timer interval, plus slack

    failures = [r.message for r in caplog.records if "header refresh failed" in r.message]
    assert failures == [], f"the header refresh raised every tick: {failures}"

