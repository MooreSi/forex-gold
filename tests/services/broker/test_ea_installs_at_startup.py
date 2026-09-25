"""A Windows start installs the latest EA when nothing is open.

Reported 2026-09-25: a fresh Windows install did not install the latest EA.
The EA was only ever deployed after Update to latest (and then only copied,
never compiled), or when the operator found the Install button. The app's
startup now hands the work to ea_deploy.install_when_idle, which does nothing
unless the book is empty (tests/services/broker/test_ea_deploy.py pins those
rules). What is pinned here is the hand-over: the open-trade count is read on
the event loop's own thread -- the database connection is per thread -- and a
count that cannot be read installs nothing.

Nothing here reaches MT5, a broker or MetaEditor.
"""
from __future__ import annotations

import asyncio

from backend.src import app
from backend.src.services.broker import ea_deploy
from backend.src.services.trading import signal_state_repo


def test_it_passes_the_open_trade_count_to_the_installer(monkeypatch):
    seen = []
    monkeypatch.setattr(signal_state_repo, "count_trade_slots_used", lambda: 0)
    monkeypatch.setattr(ea_deploy, "install_when_idle",
                        lambda slots: seen.append(slots()) or {"installed": True})

    asyncio.run(app._install_ea_at_startup())

    assert seen == [0]


def test_a_count_that_cannot_be_read_installs_nothing(monkeypatch):
    called = []

    def _boom():
        raise RuntimeError("db not ready")

    monkeypatch.setattr(signal_state_repo, "count_trade_slots_used", _boom)
    monkeypatch.setattr(ea_deploy, "install_when_idle",
                        lambda slots: called.append(1) or {"installed": True})

    asyncio.run(app._install_ea_at_startup())

    assert called == []


def test_startup_schedules_it():
    """Present in startup(), not merely defined."""
    import inspect
    assert "_install_ea_at_startup()" in inspect.getsource(app.startup)
