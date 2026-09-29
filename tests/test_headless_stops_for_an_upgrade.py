"""A headless node stops when an upgrade asks it to (2026-09-29).

Upgrade VPS ends in `os_utils.restart_app`, which spawns the relaunch (or
asks the launcher for one) and then stops the process through
`shutdown_ui()`. Headless mode registered no stopper, so `shutdown_ui()`
was a no-op there: the process kept running the old code, and the relaunch
found the single-instance lock still held. /restartapp never had this gap
(it calls os._exit itself in headless mode); the update path did.

Headless mode now registers a stopper that ends its wait, so the normal
shutdown runs and run.py exits with the code the launcher relaunches on.

Nothing here starts the app: only the stopper seam is exercised.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from backend.src.utils import os_utils

RUN_PY = Path(__file__).resolve().parent.parent / "run.py"


def _load_run_module():
    spec = importlib.util.spec_from_file_location("forex_run_headless_stop", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shutdown_ui_ends_the_headless_wait(monkeypatch):
    monkeypatch.setattr(os_utils, "_ui_stopper", None)
    run = _load_run_module()

    async def _go():
        stop_event = asyncio.Event()
        run._stop_headless_on_request(stop_event, asyncio.get_running_loop())
        stopped = os_utils.shutdown_ui()
        await asyncio.wait_for(stop_event.wait(), timeout=1)
        return stopped

    assert asyncio.run(_go()) is True


def test_shutdown_ui_works_from_another_thread(monkeypatch):
    """apply_update's steps run in worker threads; the stopper must be safe
    to call from one."""
    monkeypatch.setattr(os_utils, "_ui_stopper", None)
    run = _load_run_module()

    async def _go():
        stop_event = asyncio.Event()
        run._stop_headless_on_request(stop_event, asyncio.get_running_loop())
        await asyncio.to_thread(os_utils.shutdown_ui)
        await asyncio.wait_for(stop_event.wait(), timeout=1)

    asyncio.run(_go())
