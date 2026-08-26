"""The in-process render harness.

Shared by every test that needs the real dashboard rendered rather than
merely imported. See tests/frontend/test_main_page_renders.py for why it
exists and tests/frontend/_render/main_shim.py for what it stubs out --
in short, the real startup opens the developer's live trading database and
the real engine opens an MT5 bridge connection, and neither may happen here.
"""
from __future__ import annotations

import pathlib

import pytest_asyncio
from nicegui.testing.user_simulation import user_simulation

SHIM = pathlib.Path(__file__).parent / "_render" / "main_shim.py"


@pytest_asyncio.fixture
async def user(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREX_RENDER_TEST_DB", str(tmp_path / "render.db"))
    async with user_simulation(main_file=SHIM) as simulated:
        yield simulated
