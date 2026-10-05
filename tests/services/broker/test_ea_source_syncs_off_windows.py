"""On macOS the app still puts the repo's EA source into every terminal at start.

Reported 2026-10-01: the dashboard said the chart ran v1.08 and the app shipped
v1.09, and MetaEditor on the Mac only had v1.08 to compile. Startup hands the
work to ea_deploy.install_when_idle, which does nothing off Windows (nothing can
compile there), and the only other copy happened after "Update to latest". So a
checkout edited or pulled by hand never reached the terminal's Experts folder.

Copying the .mq5 is inert: it changes nothing that runs until someone compiles,
so it needs no empty-book wait. Compiling stays Windows-only.

Nothing here reaches MT5, a broker or MetaEditor.
"""
from __future__ import annotations

import asyncio

from backend.src import app
from backend.src.services.broker import ea_deploy
from backend.src.services.trading import signal_state_repo


def _wire(monkeypatch, result):
    copied = []
    monkeypatch.setattr(signal_state_repo, "count_book_at_stake", lambda: 0)
    monkeypatch.setattr(ea_deploy, "install_when_idle", lambda slots: result)
    monkeypatch.setattr(ea_deploy, "deploy_after_update",
                        lambda *a, **k: copied.append(1) or {"ok": True})
    return copied


def test_off_windows_the_source_is_still_copied(monkeypatch):
    copied = _wire(monkeypatch, {"installed": False, "reason": "not Windows"})

    asyncio.run(app._install_ea_at_startup())

    assert copied == [1]


def test_a_busy_book_on_windows_does_not_trigger_the_copy(monkeypatch):
    copied = _wire(monkeypatch, {"installed": False,
                                 "reason": "2 trade slot(s) open; waiting for none"})

    asyncio.run(app._install_ea_at_startup())

    assert copied == []
