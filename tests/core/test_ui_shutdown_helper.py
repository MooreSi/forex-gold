"""One place knows how to stop the web server.

`no-nicegui-in-the-backend` is a counted contract: the backend must be
runnable, testable and schedulable without a UI framework present. Three
backend modules imported nicegui against a baseline of two, and two of them
were doing the same thing -- `os_utils.restart_app` shutting the server down
after spawning the relaunch, and `bot_infra._delayed_app_shutdown` doing it
again by hand for /restartapp.

`os_utils.shutdown_ui()` is that one place. `bot_infra` calls it instead of
importing the framework itself, which takes the contract back to its baseline
and means a future change to how the UI is stopped has one site, not two.

**That change happened on 2026-09-18**, and the rule is why these tests still
exist. The server is uvicorn now, not NiceGUI, so `shutdown_ui` no longer
imports anything: `run.py` registers a stopper (`server.should_exit = True`)
and this helper calls it. One site, exactly as designed — and `utils/` still
imports nothing above itself, which a `import uvicorn` here would have broken.

Nothing here starts or stops a real server: the stopper is a recording lambda.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.utils import os_utils


@pytest.fixture
def registered_server(monkeypatch):
    """A stopper that records being called, as run.py's does for real."""
    calls = []
    os_utils.register_ui_stopper(lambda: calls.append(True))
    yield calls
    # A stopper that leaks into the next test is a server the next test does
    # not know it has.
    os_utils.register_ui_stopper(None)


@pytest.fixture
def broken_server():
    """A registered server whose stop raises -- what an already-stopped or
    wedged uvicorn looks like from here."""
    def _boom():
        raise RuntimeError("no server running")
    os_utils.register_ui_stopper(_boom)
    yield
    os_utils.register_ui_stopper(None)


def test_shutting_down_asks_the_server_to_stop(registered_server):
    assert os_utils.shutdown_ui() is True
    assert registered_server == [True]


def test_a_server_that_will_not_stop_is_reported_not_raised(broken_server):
    """Callers are mid-restart or mid-update. An exception here would abort a
    relaunch that has already been spawned, leaving nothing running."""
    assert os_utils.shutdown_ui() is False


def test_no_server_at_all_is_reported_not_raised(monkeypatch):
    """Headless mode never starts one. The backend is meant to be runnable
    without a UI present -- that is the whole point of the contract this
    helper exists to satisfy."""
    os_utils.register_ui_stopper(None)
    assert os_utils.shutdown_ui() is False


def test_the_telegram_restart_path_goes_through_the_same_helper(registered_server, monkeypatch):
    """/restartapp used to import the UI framework itself. It must not any
    more -- and after the port there is no framework to import."""
    from backend.src.services.telegram import bot_infra
    from backend.src.db import database as db_module

    monkeypatch.setattr(db_module, "get_app_config", lambda key: "0")
    monkeypatch.setattr(bot_infra.asyncio, "sleep", _noop_sleep)

    asyncio.run(bot_infra._delayed_app_shutdown(0))

    assert registered_server == [True]


async def _noop_sleep(_s, *a, **k):
    return None


def test_headless_mode_never_touches_the_ui(monkeypatch, registered_server):
    """There is no server to stop in headless mode; the relaunch
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
    assert registered_server == [], "headless must not call into the UI"


def bot_infra_os():
    import os
    return os


def test_the_backend_imports_nicegui_nowhere_at_all():
    """The contract itself, asserted directly rather than inferred from a total.

    This read `<= 2` while the pre-boot licence screens were the last two
    sites. Task 090 ported those on 2026-09-18 and the contract went to
    `enforced_at_zero`, at which point `<= 2` became a test that cannot fail:
    it would have stayed green through two NiceGUI imports creeping back in.

    Zero is the claim now, and it is asserted here as well as in the contract
    gate because this is the file that explains WHY — a backend that needs a UI
    framework present cannot be run headless, tested on CI or scheduled.
    """
    import sys as _sys
    sys_path = _sys.path
    if "." not in sys_path:
        sys_path.insert(0, ".")
    from tools.refactor_audit import import_contracts as ic

    count = ic.check().counts["no-nicegui-in-the-backend"]
    assert count == 0, (
        f"{count} backend source units import nicegui. The dashboard is React "
        "and the licence screens are server-rendered HTML; nothing in "
        "backend/ needs a UI framework."
    )
