"""Two MSG_GIT_UPDATEs in a row apply one update, not two at once.

VPS, 2026-09-26: the Mac's admin console sent MSG_GIT_UPDATE at 22:27:57 and
again at 22:28:00. The client started `_apply_git_update` for each, so two
fetch / checkout / pip / pycache sweeps ran over the same checkout together.
One sweep deleted `.venv\\Lib\\site-packages\\backports\\tarfile\\__pycache__`
while the other's rglob was inside it, and the update logged
"[WinError 3] The system cannot find the path specified" -- and skipped the EA
deploy that comes after the sweep.

No git runs and nothing restarts: apply_update and _do_restart are stubs.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.remote import _update
from backend.src.services.positions import core_app_update


@pytest.fixture
def restarts(monkeypatch):
    calls: list = []
    monkeypatch.setattr(_update, "_do_restart", lambda: calls.append("restart"))
    monkeypatch.setattr(_update.sys, "platform", "linux")
    return calls


def test_a_second_update_while_one_is_running_is_not_applied(monkeypatch, restarts):
    applied: list = []
    release = asyncio.Event()

    async def _slow_apply(restart=True):
        applied.append(restart)
        await release.wait()
        return {"ok": True}
    monkeypatch.setattr(core_app_update, "apply_update", _slow_apply)

    async def _scenario():
        first = asyncio.create_task(_update._apply_git_update())
        await asyncio.sleep(0)
        second = asyncio.create_task(_update._apply_git_update())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(_scenario())

    assert applied == [False]
    assert restarts == ["restart"]


def test_a_failed_update_does_not_block_the_next_one(monkeypatch, restarts):
    results = iter([{"ok": False, "error": "fetch failed"}, {"ok": True}])
    applied: list = []

    async def _apply(restart=True):
        applied.append(restart)
        return next(results)
    monkeypatch.setattr(core_app_update, "apply_update", _apply)

    asyncio.run(_update._apply_git_update())
    asyncio.run(_update._apply_git_update())

    assert applied == [False, False]
    assert restarts == ["restart"]
