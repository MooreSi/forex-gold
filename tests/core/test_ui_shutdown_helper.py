"""One place knows how to stop the NiceGUI server.

`no-nicegui-in-the-backend` is a counted contract: the backend must be
runnable, testable and schedulable without a UI framework present. Three
backend modules imported nicegui against a baseline of two, and two of them
were doing the same thing -- `os_utils.restart_app` shutting the server down
after spawning the relaunch, and `bot_infra._delayed_app_shutdown` doing it
again by hand for /restartapp.

`os_utils.shutdown_ui()` is that one place. `bot_infra` calls it instead of
importing nicegui itself, which takes the contract back to its baseline and
means a future change to how the UI is stopped has one site, not two.

Nothing here starts or stops a real server: nicegui is faked in sys.modules.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from backend.src.utils import os_utils


@pytest.fixture
def fake_nicegui(monkeypatch):
    """A stand-in nicegui whose app records shutdown() calls."""
    calls = []
    app = types.SimpleNamespace(shutdown=lambda: calls.append(True))
    mod = types.ModuleType("nicegui")
    mod.app = app
    monkeypatch.setitem(sys.modules, "nicegui", mod)
    return calls


@pytest.fixture
def broken_nicegui(monkeypatch):
    """nicegui present but its shutdown raises -- what a headless or
    already-stopped server looks like from here."""
    def _boom():
        raise RuntimeError("no server running")
    mod = types.ModuleType("nicegui")
    mod.app = types.SimpleNamespace(shutdown=_boom)
    monkeypatch.setitem(sys.modules, "nicegui", mod)


def test_shutting_down_asks_the_server_to_stop(fake_nicegui):
    assert os_utils.shutdown_ui() is True
    assert fake_nicegui == [True]


def test_a_server_that_will_not_stop_is_reported_not_raised(broken_nicegui):
    """Callers are mid-restart or mid-update. An exception here would abort a
    relaunch that has already been spawned, leaving nothing running."""
    assert os_utils.shutdown_ui() is False


def test_no_nicegui_at_all_is_reported_not_raised(monkeypatch):
    """The backend is meant to be runnable without a UI framework present --
    that is the whole point of the contract this helper exists to satisfy."""
    monkeypatch.setitem(sys.modules, "nicegui", None)
    assert os_utils.shutdown_ui() is False


def test_the_telegram_restart_path_goes_through_the_same_helper(fake_nicegui, monkeypatch):
    """/restartapp used to import nicegui itself. It must not any more."""
    from backend.src.services.telegram import bot_infra
    from backend.src.db import database as db_module

    monkeypatch.setattr(db_module, "get_app_config", lambda key: "0")
    monkeypatch.setattr(bot_infra.asyncio, "sleep", _noop_sleep)

    asyncio.run(bot_infra._delayed_app_shutdown(0))

    assert fake_nicegui == [True]


async def _noop_sleep(_s, *a, **k):
    return None


def test_headless_mode_never_touches_the_ui(monkeypatch, fake_nicegui):
    """There is no NiceGUI server to stop in headless mode; the relaunch
    subprocess was already spawned, so ending the process is all that is
    needed. Calling shutdown() there would be a no-op at best."""
    from backend.src.services.telegram import bot_infra
    from backend.src.db import database as db_module

    monkeypatch.setattr(db_module, "get_app_config", lambda key: "1")
    monkeypatch.setattr(bot_infra.asyncio, "sleep", _noop_sleep)

    exited = []
    monkeypatch.setattr(bot_infra_os(), "_exit", lambda code: exited.append(code))

    asyncio.run(bot_infra._delayed_app_shutdown(0))

    assert exited == [0]
    assert fake_nicegui == [], "headless must not call into the UI"


def bot_infra_os():
    import os
    return os


def test_the_backend_no_longer_imports_nicegui_in_three_places():
    """The contract itself, asserted directly rather than inferred from a total.

    Two of the remaining sites are function-local imports for genuinely
    cross-cutting actions -- the licence dialog and the UI shutdown -- which is
    why they are baselined rather than banned.
    """
    import sys as _sys
    sys_path = _sys.path
    if "." not in sys_path:
        sys_path.insert(0, ".")
    from tools.refactor_audit import import_contracts as ic

    count = ic.check().counts["no-nicegui-in-the-backend"]
    assert count <= 2, (
        f"{count} backend source units import nicegui; the baseline is 2 and "
        "bot_infra should be going through os_utils.shutdown_ui()"
    )
